# GPU 调度器 (astrbot_plugin_gpu_scheduler)

定时切换 AstrBot 模型至 DeepSeek/Ollama，释放 GPU 用于游戏。

## 功能

- **自动切换**: 工作日晚间(18:00-01:30)和周末全天 → DeepSeek，其余时间 → Ollama
- **一键切换**: `/gpu_free` 释放 GPU，`/gpu_local` 切回本地（仅超级用户）
- **启动对齐**: 重启后根据当前时间自动设置正确模式
- **插件配置同步**: 切换模型时自动更新依赖插件的 provider 引用，保持一致性
- **视觉模型保护**: 智能跳过视觉/图像相关的 provider key，避免破坏图片识别功能
- **全配置化**: 所有设置项均可在 AstrBot 插件配置面板修改，无需改代码

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

使用前需在插件配置中设置 `allowed_users` 添加有权限的用户 ID。

## 配置

安装后在 AstrBot 插件管理面板中可修改所有设置：

| 分类 | 设置项 | 说明 | 默认值 |
|------|--------|------|--------|
| **连接** | `api_url` | AstrBot API 地址 | `http://127.0.0.1:6185` |
| | `api_user` | 登录用户名 | `astrbot` |
| | `api_password` | 登录密码 | (必填) |
| **权限** | `allowed_users` | 允许使用命令的 QQ 号列表 | 空（需手动配置） |
| **调度** | `enable_schedule` | 是否启用定时调度 | ✅ |
| | `timezone` | 时区 | `Asia/Shanghai` |
| | `schedule_weekday_deepseek_time` | 工作日晚间切 DeepSeek | `18:00` |
| | `schedule_weekday_ollama_time` | 工作日早间切回 Ollama | `01:30` |
| | `enable_weekend_deepseek` | 周末全天 DeepSeek | ✅ |
| | `schedule_weekend_deepseek_time` | 周五晚切 DeepSeek | `18:00` |
| | `schedule_weekend_ollama_time` | 周一早切回 Ollama | `01:30` |
| **Providers** | `ollama_providers` | 需切换的 Ollama provider 列表 | qwen3.6, nemotron |
| | `deepseek_providers` | DeepSeek provider 列表 | v4-flash, v4-pro |
| **同步** | `enable_config_sync` | 自动同步其他插件 provider 引用 | ✅ |
| | `config_sync_skip_keys` | 同步时跳过的 key 关键词 | image, caption, vision, reverse |
| | `config_dir` | 插件配置目录 | `/AstrBot/data/config` |
| **高级** | `token_refresh_margin` | Token 提前刷新秒数 | 300 |

### 自定义示例

**示例 1: 晚上 9 点才切 DeepSeek，早上 6 点切回**
```
schedule_weekday_deepseek_time: 21:00
schedule_weekday_ollama_time: 06:00
```

**示例 2: 只用 qwen 一个模型**
```
ollama_providers: ["ollama/qwen3.6:35b"]
deepseek_providers: ["deepseek/deepseek-v4-flash"]
```

**示例 3: 关闭配置同步（只切换 provider，不动其他插件）**
```
enable_config_sync: false
```

**示例 4: 配置命令权限**
```
allowed_users: ["123456789", "987654321"]
```

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
