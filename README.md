# GPU 调度器 (astrbot_plugin_gpu_scheduler)

定时切换 AstrBot 模型至 DeepSeek/Ollama，释放 GPU 用于游戏。

## 功能

- **自动切换**: 工作日晚间(18:00-01:30)和周末全天 → DeepSeek，其余时间 → Ollama
- **一键切换**: `/gpu_free` 释放 GPU，`/gpu_local` 切回本地（仅超级用户）
- **启动对齐**: 重启后根据当前时间自动设置正确模式
- **插件配置同步**: 切换模型时自动更新依赖插件（如群聊增强、记忆等）的 provider 引用，保持一致性
- **视觉模型保护**: 智能跳过视觉/图像相关的 provider key，避免破坏图片识别功能

## 时间表

| 时段 | 模式 |
|------|------|
| 周一~周四 01:30-18:00 | Ollama 本地 |
| 周一~周四 18:00-次日 01:30 | DeepSeek |
| 周五 01:30-18:00 | Ollama 本地 |
| 周五 18:00 - 周一 01:30 | DeepSeek |

## 安装

将插件目录放入 AstrBot 的 `data/plugins/` 目录，重启 AstrBot。

```bash
cd AstrBot/data/plugins/
git clone https://github.com/fatman24/astrbot-plugin-gpu-scheduler.git
```

## 命令

- `/gpu_free` — 立即释放 GPU，切换至 DeepSeek
- `/gpu_local` — 立即切回本地 Ollama

仅 QQ 号 `1011953945` 可使用这两个命令。

## 工作原理

### Provider 切换

插件通过 AstrBot API 管理 provider 的启用/禁用状态：

- **切到 DeepSeek**: 禁用 `ollama/qwen3.6:35b` 和 `ollama/nemotron-cascade-2:latest`，启用 DeepSeek providers
- **切回 Ollama**: 重新启用 Ollama providers

### 插件配置同步

切换 provider 时，递归扫描 `data/config/` 下的所有 JSON 配置文件，将引用 Ollama provider 的 key 替换为 DeepSeek fallback（反之亦然），并自动备份/恢复原始配置。

**重要**: 包含 `image`、`caption`、`vision`、`reverse` 关键词的 key 会被跳过——这些功能依赖本地视觉模型（如 qwen3.6:35b），DeepSeek V4 Flash 不支持多模态。

## 依赖

- Python >= 3.10
- httpx >= 0.27.0
- apscheduler >= 3.10.0
- AstrBot >= 4.0

## 许可证

MIT License
