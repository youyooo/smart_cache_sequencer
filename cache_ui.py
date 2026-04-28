# SPDX-License-Identifier: GPL-3.0-or-later

"""Sidebar panel, property group, and operators for Smart Cache."""

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty

from . import __init__ as sc


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


class CACHE_PT_smart_cache(bpy.types.Panel):
    bl_label = "Smart Cache"
    bl_idname = "CACHE_PT_smart_cache"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Smart Cache"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.smart_cache

        # Master toggle
        row = layout.row()
        row.prop(settings, "enabled", text="", icon='CACHE' if settings.enabled else 'LAYER_USED')
        row.label(text="Enable Smart Cache")

        if not settings.enabled:
            return

        # Settings
        box = layout.box()
        box.label(text="Settings", icon='PREFERENCES')
        box.prop(settings, "cache_directory")
        box.prop(settings, "max_cache_size_gb")
        box.prop(settings, "prefetch_lookahead")
        box.prop(settings, "auto_cache_on_playback")
        box.prop(settings, "persistent_proxy")

        # Init singletons on demand
        manager, renderer, server, prefetch = sc.get_singletons()
        if not manager and settings.enabled:
            sc.init_singletons()
            manager, renderer, server, prefetch = sc.get_singletons()
            from . import cache_handlers
            cache_handlers.register_handlers()

        # Cache status
        if manager:
            usage = manager.get_disk_usage()
            box = layout.box()
            box.label(text="Cache Status", icon='INFO')
            row = box.row()
            row.label(text=f"Size: {usage['total_size_mb']:.1f} MB")
            row.label(text=f"/ {usage['max_size_gb']:.0f} GB")
            row = box.row()
            row.label(text=f"Frames: {usage['frame_count']}")
            row.label(text=f"Strips: {usage['strip_count']}")

            # Progress bar
            if renderer and renderer.is_rendering:
                prog = renderer.progress
                progress_pct = prog['current'] / max(prog['total'], 1)
                box.progress(progress=progress_pct)
                box.label(text=f"Caching: {prog['strip_name']} ({prog['current']}/{prog['total']})")
                box.operator("smart_cache.cancel_render", text="Cancel", icon='CANCEL')

        # Actions
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

        # Per-strip status
        if manager and manager.strip_hashes:
            box = layout.box()
            box.label(text="Cached Strips", icon='SEQUENCE')
            for strip_name, hashes in manager.strip_hashes.items():
                has_l0 = manager.strip_has_cache(strip_name, 0)
                has_l1 = manager.strip_has_cache(strip_name, 1)
                row = box.row()
                row.label(text=f"  {strip_name}")
                sub = row.row(align=True)
                sub.scale_x = 0.6
                if has_l0:
                    sub.label(text="L0", icon='CHECKBOX_HLT')
                if has_l1:
                    sub.label(text="L1", icon='CHECKBOX_HLT')


class SMART_CACHE_OT_cache_selected(bpy.types.Operator):
    bl_idname = "smart_cache.cache_selected"
    bl_label = "Cache Selected Strips"
    bl_description = "Render selected strips to disk cache"

    def execute(self, context):
        settings = context.scene.smart_cache
        manager, renderer, server, prefetch = sc.get_singletons()

        if not manager or not renderer:
            if settings.enabled:
                sc.init_singletons()
                from . import cache_handlers
                cache_handlers.register_handlers()
                manager, renderer, server, prefetch = sc.get_singletons()
            if not manager:
                self.report({'WARNING'}, "Smart Cache not initialized")
                return {'CANCELLED'}

        manager.max_size_bytes = int(settings.max_cache_size_gb * 1024 * 1024 * 1024)
        manager.ensure_dirs()

        se = context.scene.sequence_editor
        if not se:
            return {'CANCELLED'}

        count = 0
        for strip in context.selected_sequences:
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
        manager, renderer, server, prefetch = sc.get_singletons()

        if not manager or not renderer:
            if settings.enabled:
                sc.init_singletons()
                from . import cache_handlers
                cache_handlers.register_handlers()
                manager, renderer, server, prefetch = sc.get_singletons()
            if not manager:
                self.report({'WARNING'}, "Smart Cache not initialized")
                return {'CANCELLED'}

        manager.max_size_bytes = int(settings.max_cache_size_gb * 1024 * 1024 * 1024)
        manager.ensure_dirs()

        se = context.scene.sequence_editor
        if not se:
            return {'CANCELLED'}

        count = 0
        for strip in se.sequences_all:
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
        manager, renderer, server, prefetch = sc.get_singletons()
        if renderer:
            renderer.cancel_render()
        self.report({'INFO'}, "Rendering cancelled")
        return {'FINISHED'}


class SMART_CACHE_OT_toggle_cache_playback(bpy.types.Operator):
    bl_idname = "smart_cache.toggle_cache_playback"
    bl_label = "Toggle Cache Playback"
    bl_description = "Enable/disable proxy strip mode for cached playback"

    def execute(self, context):
        manager, renderer, server, prefetch = sc.get_singletons()
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
        manager, renderer, server, prefetch = sc.get_singletons()
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
        manager, renderer, server, prefetch = sc.get_singletons()
        if manager:
            manager.purge_stale()
        self.report({'INFO'}, "Stale cache purged")
        return {'FINISHED'}


classes = [
    SmartCacheSettings,
    CACHE_PT_smart_cache,
    SMART_CACHE_OT_cache_selected,
    SMART_CACHE_OT_cache_all,
    SMART_CACHE_OT_cancel_render,
    SMART_CACHE_OT_toggle_cache_playback,
    SMART_CACHE_OT_purge_cache,
    SMART_CACHE_OT_purge_stale,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
