"""
AstrBot GPU Scheduler Plugin
定时切换 AstrBot 模型至 DeepSeek/Ollama，避免游戏时 GPU 被占用

时间表：
- 周一至周四 01:30-18:00 → Ollama 本地模型
- 周一至周四 18:00-次日 01:30 → DeepSeek 云端
- 周五 01:30-18:00 → Ollama
- 周五 18:00 - 周一 01:30 → DeepSeek（周五晚+周末全天）

命令（仅 user_id=1011953945 可用）：
- /gpu_free   → 释放 GPU，切到 DeepSeek
- /gpu_local  → 切回本地 Ollama
"""

import asyncio
import json
import re
import os
import datetime
import shutil
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot import logger

# ============ 配置 ============
ASTRBOT_API = "http://127.0.0.1:6185"
ASTRBOT_USER = "astrbot"
ASTRBOT_PASS = "ZEr@1201390032"
ALLOWED_USER = 1011953945
STATE_FILE = "data/gpu_scheduler_state.json"
TOKEN_REFRESH_MARGIN = 300  # token 提前 5 分钟刷新

# 需要切换的 Ollama chat_completion providers
OLLAMA_CHAT_PROVIDERS = [
    "ollama/qwen3.6:35b",
    "ollama/nemotron-cascade-2:latest",
]

# DeepSeek providers（确保生效）
DEEPSEEK_CHAT_PROVIDERS = [
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
]

# Ollama → DeepSeek 的 fallback 映射（provider 禁用时替换用）
OLLAMA_TO_DEEPSEEK_FALLBACK = {
    "ollama/qwen3.6:35b": "deepseek/deepseek-v4-flash",
    "ollama/nemotron-cascade-2:latest": "deepseek/deepseek-v4-flash",
}

# 需要同步 provider 引用的插件配置目录
PLUGIN_CONFIG_DIR = "/AstrBot/data/config"
# 插件配置备份目录
PLUGIN_BACKUP_DIR = "data/gpu_scheduler_backups"

# ============ 插件主类 ============

@register(
    "astrbot_plugin_gpu_scheduler",
    "zeroo",
    "定时切换AI模型，释放GPU用于游戏",
    "1.0.0",
)
class GpuScheduler(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._token: str | None = None
        self._token_expiry: float = 0
        self._client = httpx.AsyncClient(timeout=15.0)
        self._mode: str = "unknown"

        # 初始化调度器
        self._scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._setup_schedule()
        self._scheduler.start()

        # 启动时对齐状态（等 AstrBot 启动完成）
        asyncio.create_task(self._startup_align())

        # 加载持久化状态
        self._load_state()
        logger.info("[GPU Scheduler] 插件已初始化")

    # ============ 定时调度 ============

    def _setup_schedule(self):
        """设置 cron 定时任务"""
        # Mon-Thu 18:00 → DeepSeek（晚间游戏时段开始）
        self._scheduler.add_job(
            self._scheduled_switch_to_deepseek,
            CronTrigger(day_of_week="mon-thu", hour=18, minute=0),
            id="ds_weekday_evening",
        )
        # Tue-Fri 01:30 → Ollama（晚间游戏时段结束）
        self._scheduler.add_job(
            self._scheduled_switch_to_ollama,
            CronTrigger(day_of_week="tue-fri", hour=1, minute=30),
            id="ollama_weekday_morning",
        )
        # Fri 18:00 → DeepSeek（周五晚+周末开始）
        self._scheduler.add_job(
            self._scheduled_switch_to_deepseek,
            CronTrigger(day_of_week="fri", hour=18, minute=0),
            id="ds_weekend_start",
        )
        # Mon 01:30 → Ollama（周末结束）
        self._scheduler.add_job(
            self._scheduled_switch_to_ollama,
            CronTrigger(day_of_week="mon", hour=1, minute=30),
            id="ollama_weekend_end",
        )
        logger.info(
            "[GPU Scheduler] 已设置定时任务: "
            "Mon-Thu 18:00→DS, Tue-Fri 01:30→Ollama, "
            "Fri 18:00→DS, Mon 01:30→Ollama"
        )

    async def _scheduled_switch_to_deepseek(self):
        logger.info("[GPU Scheduler] ⏰ 定时触发: 切换至 DeepSeek")
        await self.switch_to_deepseek()

    async def _scheduled_switch_to_ollama(self):
        logger.info("[GPU Scheduler] ⏰ 定时触发: 切换至 Ollama")
        await self.switch_to_ollama()

    # ============ 核心切换逻辑 ============

    async def switch_to_deepseek(self) -> dict:
        """
        切换到 DeepSeek 模式：
        - 禁用所有 Ollama chat providers
        - 确保 DeepSeek providers 启用
        """
        logger.info("[GPU Scheduler] 开始切换至 DeepSeek 模式...")
        results = {"success": [], "failed": [], "skipped": []}

        providers = await self._get_providers()
        if not providers:
            logger.error("[GPU Scheduler] 无法获取 provider 列表")
            return {"error": "无法获取 provider 列表"}

        for p in providers:
            pid = p.get("id", "")
            ptype = p.get("provider_type", "")

            # 只处理 chat_completion 类型
            if ptype != "chat_completion":
                continue

            try:
                if pid in OLLAMA_CHAT_PROVIDERS:
                    if p.get("enable", True):
                        await self._patch_provider_enabled(pid, False)
                        results["success"].append(f"{pid} → disabled")
                    else:
                        results["skipped"].append(f"{pid} (already off)")
                elif pid in DEEPSEEK_CHAT_PROVIDERS:
                    if not p.get("enable", False):
                        await self._patch_provider_enabled(pid, True)
                        results["success"].append(f"{pid} → enabled")
                    else:
                        results["skipped"].append(f"{pid} (already on)")
            except Exception as e:
                logger.error(f"[GPU Scheduler] 切换 {pid} 失败: {e}")
                results["failed"].append(f"{pid}: {e}")

        self._mode = "deepseek"
        self._save_state()

        # 同步插件配置：禁用 Ollama provider 后，更新插件引用到 DeepSeek fallback
        sync_result = await self._sync_plugin_providers_to_deepseek()

        logger.info(
            f"[GPU Scheduler] DeepSeek 模式切换完成: "
            f"成功={len(results['success'])}, 失败={len(results['failed'])}, 跳过={len(results['skipped'])}, "
            f"插件同步={sync_result.get('updated',0)}/{sync_result.get('total',0)}"
        )
        return results

    async def switch_to_ollama(self) -> dict:
        """
        切换到 Ollama 模式：
        - 启用所有 Ollama chat providers
        - DeepSeek providers 不动（保持可用作为备选）
        """
        logger.info("[GPU Scheduler] 开始切换至 Ollama 模式...")
        results = {"success": [], "failed": [], "skipped": []}

        providers = await self._get_providers()
        if not providers:
            logger.error("[GPU Scheduler] 无法获取 provider 列表")
            return {"error": "无法获取 provider 列表"}

        for p in providers:
            pid = p.get("id", "")
            ptype = p.get("provider_type", "")

            if ptype != "chat_completion":
                continue

            try:
                if pid in OLLAMA_CHAT_PROVIDERS:
                    if not p.get("enable", False):
                        await self._patch_provider_enabled(pid, True)
                        results["success"].append(f"{pid} → enabled")
                    else:
                        results["skipped"].append(f"{pid} (already on)")
            except Exception as e:
                logger.error(f"[GPU Scheduler] 切换 {pid} 失败: {e}")
                results["failed"].append(f"{pid}: {e}")

        self._mode = "ollama"
        self._save_state()

        # 同步插件配置：恢复插件原有的 Ollama provider 引用
        sync_result = await self._restore_plugin_providers()

        logger.info(
            f"[GPU Scheduler] Ollama 模式切换完成: "
            f"成功={len(results['success'])}, 失败={len(results['failed'])}, 跳过={len(results['skipped'])}, "
            f"插件恢复={sync_result.get('restored',0)}/{sync_result.get('total',0)}"
        )
        return results

    # ============ 插件配置同步 ============

    def _get_plugin_configs(self) -> dict:
        """加载所有插件 JSON 配置"""
        configs = {}
        try:
            if not os.path.isdir(PLUGIN_CONFIG_DIR):
                logger.warning(f"[GPU Scheduler] 配置目录不存在: {PLUGIN_CONFIG_DIR}")
                return configs
            for fname in os.listdir(PLUGIN_CONFIG_DIR):
                if not fname.endswith(".json") or ".bak" in fname:
                    continue
                fpath = os.path.join(PLUGIN_CONFIG_DIR, fname)
                try:
                    with open(fpath, "r", encoding="utf-8-sig") as f:
                        configs[fname] = json.load(f)
                except (json.JSONDecodeError, IOError):
                    pass
        except Exception as e:
            logger.error(f"[GPU Scheduler] 读取插件配置失败: {e}")
        return configs

    def _replace_provider_in_config(self, config: dict, reverse: bool = False) -> tuple[dict, int]:
        """
        递归替换配置中的 provider ID。
        reverse=False: Ollama → DeepSeek
        reverse=True: DeepSeek → Ollama（从备份恢复）
        返回: (修改后的配置, 替换数量)
        """
        count = 0
        source_map = OLLAMA_TO_DEEPSEEK_FALLBACK if not reverse else {
            v: k for k, v in OLLAMA_TO_DEEPSEEK_FALLBACK.items()
        }

        if isinstance(config, dict):
            result = {}
            for key, value in config.items():
                if isinstance(value, str):
                    # Skip vision/image/caption related keys - these use local GPU models
                    if re.search(r"image|caption|vision|reverse", key, re.IGNORECASE):
                        result[key] = value
                        continue
                    for src, dst in source_map.items():
                        if value == src:
                            result[key] = dst
                            count += 1
                            logger.info(f"[GPU Scheduler] 配置同步: {key}={src} → {dst}")
                            break
                    else:
                        result[key] = value
                elif isinstance(value, (dict, list)):
                    new_val, sub_count = self._replace_provider_in_config(value, reverse)
                    result[key] = new_val
                    count += sub_count
                else:
                    result[key] = value
            return result, count
        elif isinstance(config, list):
            result = []
            for item in config:
                if isinstance(item, (dict, list)):
                    new_item, sub_count = self._replace_provider_in_config(item, reverse)
                    result.append(new_item)
                    count += sub_count
                else:
                    result.append(item)
            return result, count
        return config, count

    async def _sync_plugin_providers_to_deepseek(self) -> dict:
        """
        将插件配置中引用 Ollama providers 的字段替换为 DeepSeek fallback。
        先备份原始配置，再修改。
        """
        result = {"total": 0, "updated": 0, "errors": 0}
        try:
            os.makedirs(PLUGIN_BACKUP_DIR, exist_ok=True)
            configs = self._get_plugin_configs()
            if not configs:
                return result

            for fname, config in configs.items():
                # 先检查是否包含需要替换的 provider ID
                raw = json.dumps(config, ensure_ascii=False)
                has_match = any(pid in raw for pid in OLLAMA_CHAT_PROVIDERS)
                if not has_match:
                    continue

                result["total"] += 1
                try:
                    # 备份原始配置
                    backup_path = os.path.join(PLUGIN_BACKUP_DIR, fname)
                    with open(backup_path, "w", encoding="utf-8") as f:
                        json.dump(config, f, ensure_ascii=False, indent=2)
                    logger.debug(f"[GPU Scheduler] 已备份: {fname}")

                    # 替换
                    new_config, count = self._replace_provider_in_config(config, reverse=False)
                    if count > 0:
                        orig_path = os.path.join(PLUGIN_CONFIG_DIR, fname)
                        with open(orig_path, "w", encoding="utf-8") as f:
                            json.dump(new_config, f, ensure_ascii=False, indent=2)
                        result["updated"] += 1
                        logger.info(
                            f"[GPU Scheduler] 插件配置已同步: {fname} ({count} 处修改)"
                        )
                except Exception as e:
                    logger.error(f"[GPU Scheduler] 同步 {fname} 失败: {e}")
                    result["errors"] += 1
        except Exception as e:
            logger.error(f"[GPU Scheduler] 插件配置同步异常: {e}")
        return result

    async def _restore_plugin_providers(self) -> dict:
        """从备份恢复插件配置（切回 Ollama 时调用）"""
        result = {"total": 0, "restored": 0, "skipped": 0, "errors": 0}
        try:
            if not os.path.isdir(PLUGIN_BACKUP_DIR):
                logger.debug("[GPU Scheduler] 无备份目录，跳过恢复")
                return result

            for fname in os.listdir(PLUGIN_BACKUP_DIR):
                if not fname.endswith(".json"):
                    continue
                result["total"] += 1
                backup_path = os.path.join(PLUGIN_BACKUP_DIR, fname)
                orig_path = os.path.join(PLUGIN_CONFIG_DIR, fname)

                try:
                    with open(backup_path, "r", encoding="utf-8") as f:
                        backup_config = json.load(f)

                    # 确认备份中有 Ollama provider 引用
                    raw = json.dumps(backup_config, ensure_ascii=False)
                    if not any(pid in raw for pid in OLLAMA_CHAT_PROVIDERS):
                        logger.debug(f"[GPU Scheduler] 跳过 {fname}: 备份中无 Ollama provider 引用")
                        result["skipped"] += 1
                        continue

                    # 恢复
                    with open(orig_path, "w", encoding="utf-8") as f:
                        json.dump(backup_config, f, ensure_ascii=False, indent=2)
                    result["restored"] += 1
                    logger.info(f"[GPU Scheduler] 已恢复: {fname}")

                    # 删除备份
                    os.remove(backup_path)
                except Exception as e:
                    logger.error(f"[GPU Scheduler] 恢复 {fname} 失败: {e}")
                    result["errors"] += 1

            # 清理空备份目录
            try:
                remaining = os.listdir(PLUGIN_BACKUP_DIR)
                if not remaining:
                    os.rmdir(PLUGIN_BACKUP_DIR)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[GPU Scheduler] 插件配置恢复异常: {e}")
        return result

    # ============ API 通信 ============

    async def _get_token(self) -> str:
        """获取/刷新 AstrBot API token"""
        now = datetime.datetime.now().timestamp()
        if self._token and now < self._token_expiry - TOKEN_REFRESH_MARGIN:
            return self._token

        try:
            r = await self._client.post(
                f"{ASTRBOT_API}/api/v1/auth/login",
                json={"username": ASTRBOT_USER, "password": ASTRBOT_PASS},
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["data"]["token"]
            self._token_expiry = datetime.datetime.now().timestamp() + 3600  # 假设 1 小时过期
            logger.debug("[GPU Scheduler] Token 已刷新")
            return self._token
        except Exception as e:
            logger.error(f"[GPU Scheduler] 登录失败: {e}")
            raise RuntimeError(f"AstrBot API 登录失败: {e}")

    async def _get_providers(self) -> list | None:
        """获取所有 provider 列表"""
        try:
            token = await self._get_token()
            r = await self._client.get(
                f"{ASTRBOT_API}/api/v1/providers",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            return r.json().get("data", {}).get("providers", [])
        except Exception as e:
            logger.error(f"[GPU Scheduler] 获取 providers 失败: {e}")
            return None

    async def _patch_provider_enabled(self, provider_id: str, enabled: bool):
        """切换单个 provider 的启用状态"""
        token = await self._get_token()
        r = await self._client.patch(
            f"{ASTRBOT_API}/api/v1/providers/{provider_id}/enabled",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"enabled": enabled},
        )
        r.raise_for_status()
        logger.debug(f"[GPU Scheduler] PATCH {provider_id} enabled={enabled} → {r.status_code}")

    # ============ 状态持久化 ============

    def _save_state(self):
        """保存当前模式到文件"""
        try:
            os.makedirs("data", exist_ok=True)
            state = {
                "mode": self._mode,
                "updated": datetime.datetime.now().isoformat(),
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[GPU Scheduler] 保存状态失败: {e}")

    def _load_state(self):
        """加载持久化状态"""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self._mode = state.get("mode", "unknown")
                logger.info(f"[GPU Scheduler] 加载状态: mode={self._mode}")
        except Exception as e:
            logger.warning(f"[GPU Scheduler] 加载状态失败: {e}")

    # ============ 启动对齐 ============

    async def _startup_align(self):
        """插件启动时，根据当前时间自动对齐模型状态"""
        await asyncio.sleep(10)  # 等 AstrBot 完全启动
        logger.info("[GPU Scheduler] 执行启动对齐...")

        now = datetime.datetime.now()
        target_mode = self._calc_target_mode(now)

        if target_mode == "deepseek":
            if self._mode != "deepseek":
                logger.info(f"[GPU Scheduler] 启动对齐: 当前={self._mode}, 目标=deepseek, 执行切换")
                await self.switch_to_deepseek()
            else:
                logger.info("[GPU Scheduler] 启动对齐: 已是 DeepSeek 模式，检查插件配置...")
                # 即使跳过 provider 切换，也要确保插件配置对齐
                sync_result = await self._sync_plugin_providers_to_deepseek()
                logger.info(
                    f"[GPU Scheduler] 启动对齐 DeepSeek: 插件同步={sync_result.get('updated',0)}/{sync_result.get('total',0)}"
                )
        else:
            if self._mode != "ollama":
                logger.info(f"[GPU Scheduler] 启动对齐: 当前={self._mode}, 目标=ollama, 执行切换")
                await self.switch_to_ollama()
            else:
                logger.info("[GPU Scheduler] 启动对齐: 已是 Ollama 模式，检查插件配置...")
                sync_result = await self._restore_plugin_providers()
                logger.info(
                    f"[GPU Scheduler] 启动对齐 Ollama: 插件恢复={sync_result.get('restored',0)}/{sync_result.get('total',0)}"
                )

    def _calc_target_mode(self, now: datetime.datetime) -> str:
        """
        根据当前时间计算目标模式。
        规则：
          - 周一 18:00 → 周二 01:30: DeepSeek
          - 周二 18:00 → 周三 01:30: DeepSeek
          - 周三 18:00 → 周四 01:30: DeepSeek
          - 周四 18:00 → 周五 01:30: DeepSeek
          - 周五 18:00 → 周一 01:30: DeepSeek (周五晚+周末)
          - 其余时间: Ollama
        """
        weekday = now.weekday()  # 0=Mon, 1=Tue, ..., 5=Sat, 6=Sun
        minutes = now.hour * 60 + now.minute

        # DeepSeek 时段开始: 18:00 = 1080 分钟
        DS_START = 18 * 60  # 1080
        # DeepSeek 时段结束: 次日 01:30 = 90 分钟
        DS_END_NEXT_DAY = 1 * 60 + 30  # 90

        if weekday == 5 or weekday == 6:
            # 周六、周日全天 DeepSeek
            return "deepseek"
        elif weekday == 4:
            # 周五: 18:00 之后 DeepSeek
            if minutes >= DS_START:
                return "deepseek"
            else:
                # 周五 01:30-18:00 → 检查是否在凌晨 DeepSeek 延续中
                # 周五 00:00-01:30 是周四晚上的延续
                if minutes <= DS_END_NEXT_DAY:
                    return "deepseek"
                return "ollama"
        elif weekday == 0:
            # 周一: 01:30 之前是周末延续 DeepSeek
            if minutes <= DS_END_NEXT_DAY:
                return "deepseek"
            elif minutes >= DS_START:
                return "deepseek"
            else:
                return "ollama"
        else:
            # 周二、三、四
            if minutes <= DS_END_NEXT_DAY:
                # 前一天晚上的延续 (01:30 之前)
                return "deepseek"
            elif minutes >= DS_START:
                # 当天晚上 18:00 之后
                return "deepseek"
            else:
                return "ollama"

    # ============ 命令处理 ============

    @filter.command("gpu_free")
    async def cmd_gpu_free(self, event: AstrMessageEvent):
        """释放 GPU：切换至 DeepSeek"""
        sender_id = self._get_user_id(event)
        if sender_id != ALLOWED_USER:
            yield event.plain_result("⛔ 无权限。仅超级用户可执行此操作。")
            return

        yield event.plain_result("🔄 正在释放 GPU，切换至 DeepSeek 云端...")
        try:
            result = await self.switch_to_deepseek()
            if "error" in result:
                yield event.plain_result(f"❌ 切换失败: {result['error']}")
                return

            success_count = len(result["success"])
            failed_count = len(result["failed"])
            details = "\n".join(
                [f"  ✅ {s}" for s in result["success"]]
                + [f"  ❌ {f}" for f in result["failed"]]
            )
            yield event.plain_result(
                f"✅ GPU 已释放！\n"
                f"成功: {success_count}, 失败: {failed_count}\n"
                f"{details}"
            )
        except Exception as e:
            logger.error(f"[GPU Scheduler] /gpu_free 失败: {e}")
            yield event.plain_result(f"❌ 操作异常: {e}")

    @filter.command("gpu_local")
    async def cmd_gpu_local(self, event: AstrMessageEvent):
        """切回本地：恢复 Ollama"""
        sender_id = self._get_user_id(event)
        if sender_id != ALLOWED_USER:
            yield event.plain_result("⛔ 无权限。仅超级用户可执行此操作。")
            return

        yield event.plain_result("🔄 正在恢复本地 Ollama 模型...")
        try:
            result = await self.switch_to_ollama()
            if "error" in result:
                yield event.plain_result(f"❌ 切换失败: {result['error']}")
                return

            success_count = len(result["success"])
            failed_count = len(result["failed"])
            details = "\n".join(
                [f"  ✅ {s}" for s in result["success"]]
                + [f"  ❌ {f}" for f in result["failed"]]
            )
            yield event.plain_result(
                f"✅ 已切回本地 Ollama！\n"
                f"成功: {success_count}, 失败: {failed_count}\n"
                f"{details}"
            )
        except Exception as e:
            logger.error(f"[GPU Scheduler] /gpu_local 失败: {e}")
            yield event.plain_result(f"❌ 操作异常: {e}")

    # ============ 工具方法 ============

    def _get_user_id(self, event: AstrMessageEvent) -> int:
        """从事件中提取发送者的 QQ 号"""
        try:
            # AstrMessageEvent 的 sender 信息在 message_obj 中
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj:
                sender = getattr(msg_obj, "sender", None)
                if sender:
                    uid = getattr(sender, "user_id", None)
                    if uid:
                        return int(uid)

            # 备用: 尝试从 unified_msg_origin 解析
            umo = getattr(event, "unified_msg_origin", "")
            if umo and ":" in umo:
                parts = umo.split(":")
                if len(parts) >= 2:
                    return int(parts[1])
        except (ValueError, TypeError, AttributeError):
            pass
        return 0

    # ============ 清理 ============

    async def _cleanup(self):
        """释放资源"""
        try:
            if self._scheduler and self._scheduler.running:
                self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._cleanup()
        logger.info("[GPU Scheduler] 插件已停止")

    def __del__(self):
        """兜底清理（非异步场景）"""
        try:
            if self._scheduler and self._scheduler.running:
                self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        # httpx AsyncClient 在非异步上下文无法 await，依赖 GC 回收
