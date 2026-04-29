# SPDX-License-Identifier: GPL-3.0-or-later

"""Sidebar panel, property group, and operators for Smart Cache."""

import bpy
import os
import shutil
import traceback
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty, EnumProperty

from . import cache_core


class SmartCacheSettings(bpy.types.PropertyGroup):
    enabled: BoolProperty(
        name="Enable Smart Cache",
        description="Enable disk caching for VSE strips",
        default=False,
    )
    cache_directory: StringProperty(
        name="Cache Directory",
        description="Directory for cached frames",
        subtype='DIR_PATH',
        default="//vse_smart_cache",
    )
    max_cache_size_gb: FloatProperty(
        name="Max Cache Size (GB)",
        description="Maximum disk space for cache",
        default=10.0,
        min=1.0,
        max=100.0,
        step=100,
    )
    prefetch_lookahead: IntProperty(
        name="Prefetch Lookahead",
        description="Frames to prefetch ahead of playhead",
        default=30,
        min=5,
        max=120,
    )
    cache_quality: IntProperty(
        name="Cache Quality",
        description="PNG quality for cached frames",
        default=90,
        min=1,
        max=100,
    )
    auto_cache_on_playback: BoolProperty(
        name="Auto-Cache on Playback",
        description="Automatically cache frames during playback",
        default=True,
    )
    persistent_proxy: BoolProperty(
        name="Persistent Proxy",
        description="Keep proxy strips active after playback stops",
        default=False,
    )
    memory_cache_limit_mb: IntProperty(
        name="Memory Cache Limit (MB)",
        description="RAM cache limit for VSE playback",
        default=512,
        min=64,
        max=8192,
        step=100,
    )
    proxy_render_size: EnumProperty(
        name="Proxy Render Size",
        description="Proxy resolution for VSE preview",
        items=[
            ('FULL', 'Full Resolution', 'Render at full resolution'),
            ('PROXY_100', '100%', 'Render at 100% proxy resolution'),
            ('PROXY_75', '75%', 'Render at 75% proxy resolution'),
            ('PROXY_50', '50%', 'Render at 50% proxy resolution'),
            ('PROXY_25', '25%', 'Render at 25% proxy resolution'),
        ],
        default='FULL',
    )


def _get_singletons_safe():
    try:
        return cache_core.get_singletons()
    except Exception:
        return (None, None, None, None)


class CACHE_PT_smart_cache(bpy.types.Panel):
    bl_label = "Smart Cache"
    bl_idname = "CACHE_PT_smart_cache"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Smart Cache"

    def draw(self, context):
        try:
            self._draw_safe(context)
        except Exception as e:
            layout = self.layout
            box = layout.box()
            box.label(text="Smart Cache ERROR", icon='ERROR')
            box.label(text=str(e))
            tb = traceback.format_exc()
            for line in tb.split('\n')[-6:]:
                if line.strip():
                    box.label(text=line[:80])

    def _draw_safe(self, context):
        layout = self.layout
        settings = context.scene.smart_cache

        # Master toggle
        row = layout.row()
        row.prop(settings, "enabled", text="", icon='FILE_CACHE' if settings.enabled else 'LAYER_USED')
        row.label(text="Enable Smart Cache")

        if not settings.enabled:
            layout.label(text="Enable to see cache controls")
            return

        # === ENABLED: show controls ===
        manager, renderer, server, prefetch = _get_singletons_safe()

        # Not initialized yet
        if not manager:
            box = layout.box()
            box.label(text="Status: Not Initialized", icon='INFO')
            box.label(text="Click Re-Initialize to start")
            row = box.row()
            row.operator("smart_cache.reinit", text="Re-Initialize", icon='FILE_REFRESH')
            return

        # === CACHE STATUS ===
        try:
            usage = manager.get_disk_usage()
            box = layout.box()
            box.label(text="Cache Status", icon='INFO')
            pct = usage['total_size_mb'] / max(usage['max_size_gb'] * 1024, 1)
            bar_len = 20
            filled = int(bar_len * min(pct, 1.0))
            empty = bar_len - filled
            box.label(text=f"[{'=' * filled}{'-' * empty}] {usage['total_size_mb']:.0f} MB / {usage['max_size_gb']:.0f} GB")
            row = box.row()
            row.label(text=f"Frames: {usage['frame_count']}")
            row.label(text=f"Strips: {usage['strip_count']}")
        except Exception:
            pass

        # Progress
        if renderer and renderer.is_rendering:
            box = layout.box()
            try:
                prog = renderer.progress
                box.progress(progress=prog['current'] / max(prog['total'], 1))
                box.label(text=f"Caching: {prog['strip_name']} ({prog['current']}/{prog['total']})")
            except Exception:
                box.label(text="Caching in progress...")
            box.operator("smart_cache.cancel_render", text="Cancel", icon='CANCEL')

        # === ACTIONS ===
        box = layout.box()
        box.label(text="Actions", icon='TOOL_SETTINGS')
        row = box.row(align=True)
        row.operator("smart_cache.cache_selected", text="Cache Selected", icon='REC')
        row.operator("smart_cache.cache_all", text="Cache All", icon='REC')

        if server:
            row = box.row()
            label = "Disable Playback" if server.is_active else "Enable Playback"
            row.operator("smart_cache.toggle_cache_playback", text=label)

        row = box.row(align=True)
        row.operator("smart_cache.purge_stale", text="Purge Stale", icon='TRASH')
        row.operator("smart_cache.purge_cache", text="Purge All", icon='X')

        # === SETTINGS ===
        box = layout.box()
        box.label(text="Settings", icon='PREFERENCES')
        row = box.row(align=True)
        row.prop(settings, "memory_cache_limit_mb")
        row.operator("smart_cache.apply_memory_cache", text="", icon='CHECKMARK')
        box.prop(settings, "max_cache_size_gb")
        box.prop(settings, "cache_quality")
        box.prop(settings, "proxy_render_size")
        box.prop(settings, "cache_directory")
        box.prop(settings, "prefetch_lookahead")
        box.prop(settings, "auto_cache_on_playback")
        box.prop(settings, "persistent_proxy")

        # Per-strip cache progress
        try:
            se = context.scene.sequence_editor
            if se:
                strip_progress = []
                for strip in se.sequences:
                    if strip.type not in ('MOVIE', 'IMAGE', 'SCENE'):
                        continue
                    if strip.mute:
                        continue
                    total = strip.frame_final_end - strip.frame_final_start + 1
                    if total <= 0:
                        continue
                    cached_l0 = len(manager.get_cached_frames(strip.name, 0))
                    has_l1 = manager.strip_has_cache(strip.name, 1)
                    strip_progress.append((strip.name, cached_l0, total, has_l1))

                if strip_progress:
                    box = layout.box()
                    box.label(text="Strip Cache Progress", icon='SEQUENCE')
                    for name, cached, total, has_l1 in strip_progress:
                        pct = cached / max(total, 1)
                        row = box.row()
                        row.label(text=f"  {name}")
                        row.label(text=f"{cached}/{total}")
                        bar = box.row()
                        bar.progress(factor=pct)
                        icons = []
                        if cached > 0:
                            icons.append("L0")
                        if has_l1:
                            icons.append("L1")
                        if icons:
                            bar.label(text=" | ".join(icons))
                        else:
                            bar.label(text="---")
        except Exception:
            pass


class SMART_CACHE_OT_reinit(bpy.types.Operator):
    bl_idname = "smart_cache.reinit"
    bl_label = "Re-Initialize Smart Cache"
    bl_description = "Re-initialize cache singletons"

    def execute(self, context):
        from . import init_singletons, cache_handlers
        ok, err = init_singletons(context.scene)
        if ok:
            cache_handlers.register_handlers()
            self.report({'INFO'}, "Smart Cache initialized successfully")
        else:
            self.report({'ERROR'}, f"Init failed: {err}")
        return {'FINISHED'}


class SMART_CACHE_OT_cache_selected(bpy.types.Operator):
    bl_idname = "smart_cache.cache_selected"
    bl_label = "Cache Selected Strips"
    bl_description = "Render selected strips to disk cache"

    def execute(self, context):
        settings = context.scene.smart_cache
        manager, renderer, server, prefetch = _get_singletons_safe()

        if not manager or not renderer:
            if settings.enabled:
                from . import init_singletons, cache_handlers
                try:
                    ok = init_singletons(context.scene)
                    if ok:
                        cache_handlers.register_handlers()
                        manager, renderer, server, prefetch = _get_singletons_safe()
                except Exception as e:
                    self.report({'ERROR'}, f"Init failed: {e}")
                    return {'CANCELLED'}
            if not manager:
                self.report({'WARNING'}, "Smart Cache not initialized")
                return {'CANCELLED'}

        manager.max_size_bytes = int(settings.max_cache_size_gb * 1024 * 1024 * 1024)
        manager.ensure_dirs()

        se = context.scene.sequence_editor
        if not se:
            self.report({'WARNING'}, "No sequence editor found")
            return {'CANCELLED'}

        count = 0
        for strip in se.selected_sequences:
            if strip.type not in ('MOVIE', 'IMAGE', 'SCENE'):
                continue
            if strip.mute:
                continue
            start = strip.frame_final_start
            end = strip.frame_final_end
            renderer.queue_strip_range(strip, start, end, 0)
            if hasattr(strip, 'modifiers') and len(strip.modifiers) > 0:
                renderer.queue_strip_range(strip, start, end, 1)
            count += 1

        if count > 0:
            renderer.start_render()
            self.report({'INFO'}, f"Queued {count} strip(s) for caching")
        else:
            self.report({'WARNING'}, "No cacheable strips selected")

        return {'FINISHED'}


class SMART_CACHE_OT_cache_all(bpy.types.Operator):
    bl_idname = "smart_cache.cache_all"
    bl_label = "Cache All Strips"
    bl_description = "Render all strips in the timeline to disk cache"

    def execute(self, context):
        settings = context.scene.smart_cache
        manager, renderer, server, prefetch = _get_singletons_safe()

        if not manager or not renderer:
            if settings.enabled:
                from . import init_singletons, cache_handlers
                try:
                    ok = init_singletons(context.scene)
                    if ok:
                        cache_handlers.register_handlers()
                        manager, renderer, server, prefetch = _get_singletons_safe()
                except Exception as e:
                    self.report({'ERROR'}, f"Init failed: {e}")
                    return {'CANCELLED'}
            if not manager:
                self.report({'WARNING'}, "Smart Cache not initialized")
                return {'CANCELLED'}

        manager.max_size_bytes = int(settings.max_cache_size_gb * 1024 * 1024 * 1024)
        manager.ensure_dirs()

        se = context.scene.sequence_editor
        if not se:
            self.report({'WARNING'}, "No sequence editor found")
            return {'CANCELLED'}

        count = 0
        for strip in se.sequences:
            if strip.type not in ('MOVIE', 'IMAGE', 'SCENE'):
                continue
            if strip.mute:
                continue
            start = strip.frame_final_start
            end = strip.frame_final_end
            renderer.queue_strip_range(strip, start, end, 0)
            if hasattr(strip, 'modifiers') and len(strip.modifiers) > 0:
                renderer.queue_strip_range(strip, start, end, 1)
            count += 1

        if count > 0:
            renderer.start_render()
            self.report({'INFO'}, f"Queued {count} strip(s) for caching")
        else:
            self.report({'WARNING'}, "No cacheable strips found")

        return {'FINISHED'}


class SMART_CACHE_OT_cancel_render(bpy.types.Operator):
    bl_idname = "smart_cache.cancel_render"
    bl_label = "Cancel Rendering"
    bl_description = "Stop background cache rendering"

    def execute(self, context):
        manager, renderer, server, prefetch = _get_singletons_safe()
        if renderer:
            renderer.cancel_render()
        self.report({'INFO'}, "Rendering cancelled")
        return {'FINISHED'}


class SMART_CACHE_OT_toggle_cache_playback(bpy.types.Operator):
    bl_idname = "smart_cache.toggle_cache_playback"
    bl_label = "Toggle Cache Playback"
    bl_description = "Enable/disable proxy strip mode for cached playback"

    def execute(self, context):
        manager, renderer, server, prefetch = _get_singletons_safe()
        if not server:
            self.report({'WARNING'}, "Smart Cache not initialized")
            return {'CANCELLED'}
        if server.is_active:
            server.disable_cache_playback(context)
            self.report({'INFO'}, "Cache playback disabled")
        else:
            server.enable_cache_playback(context)
            self.report({'INFO'}, "Cache playback enabled")
        return {'FINISHED'}


class SMART_CACHE_OT_purge_cache(bpy.types.Operator):
    bl_idname = "smart_cache.purge_cache"
    bl_label = "Purge All Cache"
    bl_description = "Delete all cached frames"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        manager, renderer, server, prefetch = _get_singletons_safe()
        if server and server.is_active:
            server.disable_cache_playback(context)
        if manager:
            manager.purge_all()
        self.report({'INFO'}, "All cache purged")
        return {'FINISHED'}


class SMART_CACHE_OT_purge_stale(bpy.types.Operator):
    bl_idname = "smart_cache.purge_stale"
    bl_label = "Purge Stale Cache"
    bl_description = "Delete cache for strips that no longer exist"

    def execute(self, context):
        manager, renderer, server, prefetch = _get_singletons_safe()
        if manager:
            manager.purge_stale()
        self.report({'INFO'}, "Stale cache purged")
        return {'FINISHED'}


class SMART_CACHE_OT_apply_memory_cache(bpy.types.Operator):
    bl_idname = "smart_cache.apply_memory_cache"
    bl_label = "Apply Memory Cache Limit"
    bl_description = "Set VSE memory cache limit"

    def execute(self, context):
        settings = context.scene.smart_cache
        try:
            se = context.scene.sequence_editor
            if se:
                se.cache_memory_limit = settings.memory_cache_limit_mb
                self.report({'INFO'}, f"Memory cache limit set to {settings.memory_cache_limit_mb} MB")
            else:
                self.report({'WARNING'}, "No sequence editor found")
        except Exception as e:
            self.report({'WARNING'}, f"Could not set memory cache limit: {e}")
        return {'FINISHED'}


class SMART_CACHE_OT_open_cache_dir(bpy.types.Operator):
    bl_idname = "smart_cache.open_cache_dir"
    bl_label = "Open Cache Directory"
    bl_description = "Open the cache folder in file explorer"

    def execute(self, context):
        manager, renderer, server, prefetch = _get_singletons_safe()
        if manager:
            cache_dir = manager.base_dir
            if os.path.exists(cache_dir):
                if os.name == 'nt':
                    os.startfile(cache_dir)
                self.report({'INFO'}, f"Opened: {cache_dir}")
            else:
                self.report({'WARNING'}, "Cache directory does not exist")
        return {'FINISHED'}


class SMART_CACHE_OT_build_proxies(bpy.types.Operator):
    bl_idname = "smart_cache.build_proxies"
    bl_label = "Build Proxies for All"
    bl_description = "Enable and rebuild proxy files for all video strips"

    def execute(self, context):
        se = context.scene.sequence_editor
        if not se:
            return {'CANCELLED'}
        count = 0
        for strip in se.sequences:
            if strip.type in ('MOVIE', 'IMAGE'):
                strip.use_proxy = True
                strip.proxy.build_25 = True
                strip.proxy.build_50 = True
                strip.proxy.build_75 = False
                strip.proxy.build_100 = False
                count += 1
        if count > 0:
            bpy.ops.sequencer.rebuild_proxy()
            self.report({'INFO'}, f"Proxy rebuild started for {count} strip(s)")
        else:
            self.report({'WARNING'}, "No video strips found")
        return {'FINISHED'}


class SMART_CACHE_OT_clear_cache_memory(bpy.types.Operator):
    bl_idname = "smart_cache.clear_cache_memory"
    bl_label = "Clear Cache Memory"
    bl_description = "Purge all VSE memory and disk cache"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            bpy.ops.sequencer.clear_proxy_cache()
        except Exception:
            pass
        manager, renderer, server, prefetch = _get_singletons_safe()
        if server and server.is_active:
            server.disable_cache_playback(context)
        if manager:
            manager.purge_all()
        self.report({'INFO'}, "All cache memory and disk cache cleared")
        return {'FINISHED'}


classes = [
    SmartCacheSettings,
    CACHE_PT_smart_cache,
    SMART_CACHE_OT_reinit,
    SMART_CACHE_OT_cache_selected,
    SMART_CACHE_OT_cache_all,
    SMART_CACHE_OT_cancel_render,
    SMART_CACHE_OT_toggle_cache_playback,
    SMART_CACHE_OT_purge_cache,
    SMART_CACHE_OT_purge_stale,
    SMART_CACHE_OT_apply_memory_cache,
    SMART_CACHE_OT_open_cache_dir,
    SMART_CACHE_OT_build_proxies,
    SMART_CACHE_OT_clear_cache_memory,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
