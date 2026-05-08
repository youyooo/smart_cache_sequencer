# Smart Cache Sequencer — 开发文档

## 项目概述

Blender VSE（视频序列编辑器）的磁盘缓存插件，类似 Adobe After Effects 的缓存系统。将 VSE 片段逐帧渲染为 PNG 缓存到磁盘，支持 LRU 淘汰、修改器哈希校验、代理回放、预取缓存。

## 当前状态 (v1.1.0)

| 模块 | 状态 | 备注 |
|------|------|------|
| cache_key.py | ✅ 完成 | Hash 生成逻辑正确，位置无关哈希设计良好 |
| cache_manager.py | ✅ 完成 | LRU 淘汰、索引持久化、RAM L0 热帧 + SSD L1 温帧 + L2 按需解算 |
| cache_render.py | ✅ 完成 | 独立渲染函数 + RAM 提升集成、On-Demand 渲染 |
| cache_serve.py | ✅ 完成 | `image_strip_add` 操作符创建代理，代理回放 |
| cache_prefetch.py | ✅ 完成 | 播放头预取、空闲渲染、会话恢复 |
| cache_system.py | ✅ 完成 | 接管/恢复原生 VSE 缓存 |
| cache_handlers.py | ✅ 完成 | 帧变化、Depsgraph、保存/加载、渲染前、空闲定时器 |
| cache_ui.py | ✅ 完成 | 面板 UI 完整，分层缓存状态、命中统计 |
| __init__.py | ✅ 完成 | 生命周期管理，防重复初始化 |

## 架构

```
用户操作 → UI 面板 (cache_ui.py)
              ↓
        开关触发 → init_singletons()
              ↓
    ┌──────────┴──────────┐
    │    CacheManager     │  ← 磁盘索引、LRU 淘汰
    │    (cache_manager)  │
    └──────────┬──────────┘
         ↓              ↓
    CacheRender     CacheServe
    (逐帧渲染)      (代理回放)
         ↓              ↓
    ┌──────────┴──────────┐
    │     cache_key.py    │  ← 哈希校验（位置无关）
    └─────────────────────┘
              ↓
    cache_handlers.py  ← 帧变化、Depsgraph、保存/加载事件
```

### 数据流

```
1. 用户点击 "Cache All" 或自动触发
2. CacheRenderManager.queue_strip_range() → 遍历帧范围
3. bpy.app.timers 逐帧调用 _render_timer()
4. 每帧：设置上下文 → bpy.ops.render.opengl(sequencer=True) → 写 PNG
5. cache_manager.record_cached_frame() → 记录索引
6. 回放时：CachePlaybackController 创建代理 Image Sequence 替换原片段
```

## 核心实现方案（带技术路线）

### 问题 1：帧渲染不可靠（最高优先级）

**现状：**
- 用 `bpy.ops.render.opengl(sequencer=True)` 在 timer 里逐帧渲染
- Blender 的 `bpy.ops` 在 timer 回调中上下文经常丢失
- context override 传递 window/screen/area 不可靠

**方案 A（推荐）：`bpy.ops.render.opengl()` + 专用窗口**

不在 timer 里直接调 ops，而是：
1. 创建一个临时的 `SEQUENCE_EDITOR` 类型 area（或复用一个已有的）
2. 在渲染前确保该 area 存在且上下文有效
3. 使用 `context.temp_override()` 替代手动构造 override dict

```python
# Blender 4.0+ 支持 temp_override
with bpy.context.temp_override(window=win, area=area, region=region):
    bpy.ops.render.opengl(animation=False, sequencer=True, write_still=True)
```

**方案 B（备选）：Render API（非 ops）**

如果不走 `bpy.ops`，可以用 `bpy.types.RenderEngine.render()` 直接调用渲染引擎：
- 获取当前场景
- 设置 `scene.render.use_sequencer = True`
- 调用 `bpy.types.RenderEngine.render(scene)`（需要找到激活的渲染引擎实例）
- 这个不走 ops 系统，没有上下文问题

**方案 C（备选）：headless 渲染**

在单独的 Blender 进程中渲染：
- 保存 blend 文件副本
- `blender -b blendfile -s frame -e frame -o output -f frame`
- 读取输出结果
- 优点：百分百可靠；缺点：启动慢，不适合逐帧

### 问题 2：代理回放不工作（高优先级）

**现状：**
- `se.strips.new_image()` 创建代理 Image Sequence
- 但设置 `use_animation = True` 和 `frame_final_duration` 后不播放

**原因分析：**
- Blender 的 Image Sequence strip 需要正确的文件命名约定 `name_####.ext`
- `frame_start / frame_offset_start / frame_final_duration` 关系复杂
- 缓存可能没有完整的连续帧序列（中间有未缓存的帧）

**方案：**
1. 确保缓存文件名严格遵循 Blender 图像序列命名：`{safe_name}_{frame:04d}.png`
2. `strips.new_image()` 时只传第一帧路径
3. 正确设置代理 strip 参数：
```python
proxy = se.strips.new_image(
    name=proxy_name,
    filepath=first_frame_path,  # 只传第一帧
    channel=proxy_channel,
    frame_start=strip.frame_final_start,
)
# Blender 自动将目录中相同命名的序列识别为 Image Sequence
# 关键：文件名需要以 ####.png 结尾，frame_start 从序列起始帧设置
proxy.frame_final_duration = cached_frame_count
```

### 问题 3：自动缓存和预取

**现状：**
- `auto_cache_all()` 在初始化时将所有 strip 加入队列
- `PrefetchManager.on_frame_change()` 在播放时预取
- 但 Timer-based 渲染不可靠导致这些功能也失效

**依赖关系：问题 1 解决后自然解决。**

## 实现计划（分4个里程碑，M1-M4 ✅ 已完成）

### M1：修复渲染管线（核心） ✅
- 改用 `bpy.ops.render.render(write_still=True)` 为主方案，无需 viewport
- `render.opengl(sequencer=True)` 通过 `temp_override()` 作为回退
- 临时 Camera 兜底（场景无摄像机时自动创建）
- 验证：background 模式下成功渲染 PNG 444KB 到磁盘

### M2：修复代理回放 ✅
- 用 `image_strip_add` 操作符创建 Image Sequence，支持序列自动检测
- `temp_override()` 确保操作符上下文有效
- 缓存帧数设置 `frame_final_duration`

### M3：完善自动化 ✅
- 防重复初始化（先清理旧 singleton）
- None context 兜底清理
- 生命周期管理完善
- 版本 1.1.0

### M4：质量打磨与分层缓存（M1-M4 ✅ 已完成）
- [x] 接管原生 VSE 缓存（cache_system.py）
- [x] 分层缓存策略：RAM L0 热帧 + SSD L1 温帧 + L2 按需解算
- [x] 缓存预热与智能预取（cache_prefetch.py）
- [x] 1000+ 帧测试方案就绪（Background 模式渲染验证通过）
- [x] 防重复初始化、Cleanup 覆盖所有路径

### M5：格式多样化与监控（✅ 已完成）
- [x] JPEG/EXR 格式支持（cache_render.py + cache_manager.py + cache_ui.py）
- [x] 每 strip 独立格式选择 + 条件显示质量参数
- [x] Performance Monitor 面板（命中率、渲染耗时、磁盘预测）
- [x] 每 strip 详细统计（覆盖率、大小、格式）
- [x] Blender background 模式全导入验证通过

### M6：音视频轨道联动（✅ 已完成）
- [x] 轨道管理系统：音视频自动配对、通道同步、锁定/独奏（cache_track.py）
- [x] 波形叠层集成与 UI 配置（cache_waveform.py + cache_ui.py）
- [x] 音频感知缓存策略：有声帧优先、静音段跳过、运行时配置
- [x] 5 元组单例架构扩展

## 关键文件

| 文件 | 职责 |
|------|------|
| `__init__.py` | 插件入口，bl_info，init/cleanup 生命周期 |
| `cache_core.py` | Singleton 存储 |
| `cache_key.py` | 位置无关哈希生成 |
| `cache_manager.py` | 磁盘管理、LRU、索引持久化 |
| `cache_render.py` | 帧渲染管线（**主战场**）、On-Demand 渲染 |
| `cache_serve.py` | 代理回放 |
| `cache_prefetch.py` | 播放头预取、空闲渲染、会话恢复 |
| `cache_system.py` | 原生缓存接管/恢复管理 |
| `cache_handlers.py` | Blender 事件钩子、空闲定时器 |
| `cache_ui.py` | 面板 UI、PropertyGroup、Operators |

## 验证方法

```bash
# 语法验证
"/Applications/Blender 5.1.app/Contents/MacOS/blender" --background --python __init__.py

# 手动测试
# 1. VSE 中导入一个视频/图片序列
# 2. 侧边栏 → Smart Cache → Enable
# 3. 观察控制台日志
# 4. Cache All → 查看 strips/ 目录下是否生成 PNG
# 5. Enable Playback → 观察代理是否替换原片段
# 6. 修改片段 → 观察缓存自动失效
# 7. Ctrl+Z 撤销测试
```

## 技术债

- `bl_info["blender"]` 写的是 `(4, 4, 0)`，但实际用了 `sequencer=True` 参数（5.1+），需要确认最低版本
- 有些 `print()` 日志应该换成 `bpy.app.logger` 或 `self.report()`
- `cache_serve.py` 里 `enable_cache_playback` 缺少 `context_override` 保护
- `cache_manager.py` 的 `_find_strip_by_name()` 依赖 `bpy.context.scene`，在非主线程可能为 None
