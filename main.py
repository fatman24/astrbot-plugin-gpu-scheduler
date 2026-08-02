"""
AstrBot GPU Scheduler Plugin
定时切换 AstrBot 模型至 DeepSeek/Ollama，避免游戏时 GPU 被占用

所有设置项均可在 AstrBot 插件配置面板中修改，无需改代码：
  - API 连接信息、权限用户
  - 调度时间表、时区
  - Provider 列表（Ollama / DeepSeek）
  - 插件配置同步开关与跳过规则

命令：
  - /gpu_free   → 释放 GPU，切到 DeepSeek
  - /gpu_local  → 切回本地 Ollama
"""

import asyncio
import json
import re
import os
import datetime
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot import logger

# ============ 默认配置（与 _conf_schema.json 保持一致）============
_CONFIG_DEFAULTS = {
    "api_url": "http://127.0.0.1:6185",
    "api_user": "astrbot",
    "api_password": "",
    "allowed_users": [],
    "enable_schedule": True,
    "timezone": "Asia/Shanghai",
    "schedule_weekday_deepseek_time": "18:00",
    "schedule_weekday_ollama_time": "01:30",
    "enable_weekend_deepseek": True,
    "schedule_weekend_deepseek_time": "18:00",
    "schedule_weekend_ollama_time": "01:30",
    "ollama_providers": [
        "ollama/qwen3.6:35b",
        "ollama/nemotron-cascade-2:latest",
    ],
    "deepseek_providers": [
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    ],
    "enable_config_sync": True,
    "config_sync_skip_keys": ["image", "caption", "vision", "reverse"],
    "config_dir": "/AstrBot/data/config",
    "backup_dir": "data/gpu_scheduler_backups",
    "token_refresh_margin": 300,
}

STATE_FILE = "data/gpu_scheduler_state.json"


# ============ 插件主类 ============

@register(
    "astrbot_plugin_gpu_scheduler",
    "zeroo",
    "定时切换AI模型，释放GPU用于游戏。支持自定义时间表、Provider列表、权限控制",
    "1.1.0",
)
class GpuScheduler(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._token: str | None = None
        self._token_expiry: float = 0
        self._client = httpx.AsyncClient(timeout=15.0)
        self._mode: str = "unknown"
        self._scheduler: AsyncIOScheduler | None = None

        # 初始化调度器
        self._setup_schedule()

        # 启动时对齐状态（等 AstrBot 启动完成）
        asyncio.create_task(self._startup_align())

        # 加载持久化状态
        self._load_state()
        logger.info("[GPU Scheduler] 插件已初始化")

    # ============ 配置读取 ============

    def _cfg(self, key):
        """读取插件配置，自动回退到 _conf_schema.json 默认值"""
        try:
            val = self.config.get(key) if hasattr(self, "config") and self.config else None
        except Exception:
            val = None
        if val is not None and val != "" and val != []:
            return val
        return _CONFIG_DEFAULTS.get(key)

    def _parse_time(self, time_str: str) -> tuple[int, int]:
        """解析 HH:MM 字符串为 (hour, minute)"""
        parts = str(time_str).strip().split(":")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else (int(parts[0]), 0)

    def _get_ollama_providers(self) -> list:
        """获取 Ollama provider 列表"""
        providers = self._cfg("ollama_providers") or _CONFIG_DEFAULTS["ollama_providers"]
        return [str(p) for p in providers if p]

    def _get_deepseek_providers(self) -> list:
        """获取 DeepSeek provider 列表"""
        providers = self._cfg("deepseek_providers") or _CONFIG_DEFAULTS["deepseek_providers"]
        return [str(p) for p in providers if p]

    def _build_fallback_mapping(self) -> dict:
        """自动生成 Ollama → DeepSeek fallback 映射"""
        ollama = self._get_ollama_providers()
        deepseek = self._get_deepseek_providers()
        default_ds = deepseek[0] if deepseek else "deepseek/deepseek-v4-flash"
        return {o: default_ds for o in ollama}

    def _get_skip_keys_pattern(self) -> str:
        """构建 skip key 正则模式"""
        keys = self._cfg("config_sync_skip_keys") or _CONFIG_DEFAULTS["config_sync_skip_keys"]
        if not keys:
            return r"(?!)"  # 不跳过任何 key
        return "|".join(re.escape(str(k)) for k in keys if k)

    def _get_allowed_users(self) -> set:
        """获取允许使用命令的用户 ID 集合"""
        users = self._cfg("allowed_users") or []
        result = {1011953945}  # 始终包含默认超级用户
        for u in users:
            try:
                result.add(int(str(u).strip()))
            except (ValueError, TypeError):
                pass
        return result

    # ============ 定时调度 ============

    def _setup_schedule(self):
        """设置 cron 定时任务（根据配置）"""
        if not self._cfg("enable_schedule"):
            self._scheduler = None
            logger.info("[GPU Scheduler] ⚠ 定时调度已禁用，仅支持手动命令")
            return

        tz = self._cfg("timezone") or "Asia/Shanghai"

        # 解析时间
        wd_ds_h, wd_ds_m = self._parse_time(self._cfg("schedule_weekday_deepseek_time"))
        wd_ol_h, wd_ol_m = self._parse_time(self._cfg("schedule_weekday_ollama_time"))

        self._scheduler = AsyncIOScheduler(timezone=tz)

        # Mon-Thu → DeepSeek（晚间游戏时段开始）
        self._scheduler.add_job(
            self._scheduled_switch_to_deepseek,
            CronTrigger(day_of_week="mon-thu", hour=wd_ds_h, minute=wd_ds_m, timezone=tz),
            id="ds_weekday_evening",
        )
        # Tue-Fri → Ollama（晚间游戏时段结束）
        self._scheduler.add_job(
            self._scheduled_switch_to_ollama,
            CronTrigger(day_of_week="tue-fri", hour=wd_ol_h, minute=wd_ol_m, timezone=tz),
            id="ollama_weekday_morning",
        )

        schedule_desc = (
            f"Mon-Thu {wd_ds_h:02d}:{wd_ds_m:02d}→DS, "
            f"Tue-Fri {wd_ol_h:02d}:{wd_ol_m:02d}→Ollama"
        )

        if self._cfg("enable_weekend_deepseek"):
            we_ds_h, we_ds_m = self._parse_time(self._cfg("schedule_weekend_deepseek_time"))
            we_ol_h, we_ol_m = self._parse_time(self._cfg("schedule_weekend_ollama_time"))

            self._scheduler.add_job(
                self._scheduled_switch_to_deepseek,
                CronTrigger(day_of_week="fri", hour=we_ds_h, minute=we_ds_m, timezone=tz),
                id="ds_weekend_start",
            )
            self._scheduler.add_job(
                self._scheduled_switch_to_ollama,
                CronTrigger(day_of_week="mon", hour=we_ol_h, minute=we_ol_m, timezone=tz),
                id="ollama_weekend_end",
            )
            schedule_desc += (
                f", Fri {we_ds_h:02d}:{we_ds_m:02d}→DS, "
                f"Mon {we_ol_h:02d}:{we_ol_m:02d}→Ollama"
            )

        self._scheduler.start()
        logger.info(f"[GPU Scheduler] 已设置定时任务 (tz={tz}): {schedule_desc}")

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

        ollama_list = self._get_ollama_providers()
        deepseek_list = self._get_deepseek_providers()

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
                if pid in ollama_list:
                    if p.get("enable", True):
                        await self._patch_provider_enabled(pid, False)
                        results["success"].append(f"{pid} → disabled")
                    else:
                        results["skipped"].append(f"{pid} (already off)")
                elif pid in deepseek_list:
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

        # 同步插件配置
        sync_result = await self._sync_plugin_providers_to_deepseek()

        logger.info(
            f"[GPU Scheduler] DeepSeek 模式切换完成: "
            f"成功={len(results['success'])}, 失败={len(results['failed'])}, 跳过={len(results['skipped'])}, "
            f"插件同步={sync_result.get('updated', 0)}/{sync_result.get('total', 0)}"
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

        ollama_list = self._get_ollama_providers()

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
                if pid in ollama_list:
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

        # 同步插件配置
        sync_result = await self._restore_plugin_providers()

        logger.info(
            f"[GPU Scheduler] Ollama 模式切换完成: "
            f"成功={len(results['success'])}, 失败={len(results['failed'])}, 跳过={len(results['skipped'])}, "
            f"插件恢复={sync_result.get('restored', 0)}/{sync_result.get('total', 0)}"
        )
        return results

    # ============ 插件配置同步 ============

    def _get_config_dir(self) -> str:
        """获取插件配置目录"""
        return self._cfg("config_dir") or _CONFIG_DEFAULTS["config_dir"]

    def _get_backup_dir(self) -> str:
        """获取备份目录"""
        return self._cfg("backup_dir") or _CONFIG_DEFAULTS["backup_dir"]

    def _get_plugin_configs(self) -> dict:
        """加载所有插件 JSON 配置"""
        configs = {}
        cfg_dir = self._get_config_dir()
        try:
            if not os.path.isdir(cfg_dir):
                logger.warning(f"[GPU Scheduler] 配置目录不存在: {cfg_dir}")
                return configs
            for fname in os.listdir(cfg_dir):
                if not fname.endswith(".json") or ".bak" in fname:
                    continue
                fpath = os.path.join(cfg_dir, fname)
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
        fallback = self._build_fallback_mapping()
        source_map = fallback if not reverse else {v: k for k, v in fallback.items()}
        skip_pattern = self._get_skip_keys_pattern()

        if isinstance(config, dict):
            result = {}
            for key, value in config.items():
                if isinstance(value, str):
                    # 跳过视觉/图像/字幕等相关 key
                    if re.search(skip_pattern, key, re.IGNORECASE):
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
        """将插件配置中引用 Ollama providers 的字段替换为 DeepSeek fallback"""
        if not self._cfg("enable_config_sync"):
            return {"total": 0, "updated": 0, "skipped": "disabled"}

        result = {"total": 0, "updated": 0, "errors": 0}
        cfg_dir = self._get_config_dir()
        backup_dir = self._get_backup_dir()
        ollama_list = self._get_ollama_providers()

        try:
            os.makedirs(backup_dir, exist_ok=True)
            configs = self._get_plugin_configs()
            if not configs:
                return result

            for fname, config in configs.items():
                raw = json.dumps(config, ensure_ascii=False)
                has_match = any(pid in raw for pid in ollama_list)
                if not has_match:
                    continue

                result["total"] += 1
                try:
                    backup_path = os.path.join(backup_dir, fname)
                    with open(backup_path, "w", encoding="utf-8") as f:
                        json.dump(config, f, ensure_ascii=False, indent=2)
                    logger.debug(f"[GPU Scheduler] 已备份: {fname}")

                    new_config, count = self._replace_provider_in_config(config, reverse=False)
                    if count > 0:
                        orig_path = os.path.join(cfg_dir, fname)
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
        """从备份恢复插件配置"""
        if not self._cfg("enable_config_sync"):
            return {"total": 0, "restored": 0, "skipped": "disabled"}

        result = {"total": 0, "restored": 0, "skipped": 0, "errors": 0}
        backup_dir = self._get_backup_dir()
        cfg_dir = self._get_config_dir()
        ollama_list = self._get_ollama_providers()

        try:
            if not os.path.isdir(backup_dir):
                logger.debug("[GPU Scheduler] 无备份目录，跳过恢复")
                return result

            for fname in os.listdir(backup_dir):
                if not fname.endswith(".json"):
                    continue
                result["total"] += 1
                backup_path = os.path.join(backup_dir, fname)
                orig_path = os.path.join(cfg_dir, fname)

                try:
                    with open(backup_path, "r", encoding="utf-8") as f:
                        backup_config = json.load(f)

                    raw = json.dumps(backup_config, ensure_ascii=False)
                    if not any(pid in raw for pid in ollama_list):
                        logger.debug(f"[GPU Scheduler] 跳过 {fname}: 无 Ollama 引用")
                        result["skipped"] += 1
                        continue

                    with open(orig_path, "w", encoding="utf-8") as f:
                        json.dump(backup_config, f, ensure_ascii=False, indent=2)
                    result["restored"] += 1
                    logger.info(f"[GPU Scheduler] 已恢复: {fname}")

                    os.remove(backup_path)
                except Exception as e:
                    logger.error(f"[GPU Scheduler] 恢复 {fname} 失败: {e}")
                    result["errors"] += 1

            try:
                remaining = os.listdir(backup_dir)
                if not remaining:
                    os.rmdir(backup_dir)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[GPU Scheduler] 插件配置恢复异常: {e}")
        return result

    # ============ API 通信 ============

    async def _get_token(self) -> str:
        """获取/刷新 AstrBot API token"""
        now = datetime.datetime.now().timestamp()
        margin = self._cfg("token_refresh_margin") or _CONFIG_DEFAULTS["token_refresh_margin"]
        if self._token and now < self._token_expiry - margin:
            return self._token

        api_url = self._cfg("api_url") or _CONFIG_DEFAULTS["api_url"]
        api_user = self._cfg("api_user") or _CONFIG_DEFAULTS["api_user"]
        api_pass = self._cfg("api_password") or _CONFIG_DEFAULTS["api_password"]

        if not api_pass:
            raise RuntimeError("AstrBot API 密码未配置！请在插件设置中填写 api_password")

        try:
            r = await self._client.post(
                f"{api_url.rstrip('/')}/api/v1/auth/login",
                json={"username": api_user, "password": api_pass},
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["data"]["token"]
            self._token_expiry = datetime.datetime.now().timestamp() + 3600
            logger.debug("[GPU Scheduler] Token 已刷新")
            return self._token
        except Exception as e:
            logger.error(f"[GPU Scheduler] 登录失败: {e}")
            raise RuntimeError(f"AstrBot API 登录失败: {e}")

    async def _get_providers(self) -> list | None:
        """获取所有 provider 列表"""
        api_url = self._cfg("api_url") or _CONFIG_DEFAULTS["api_url"]
        try:
            token = await self._get_token()
            r = await self._client.get(
                f"{api_url.rstrip('/')}/api/v1/providers",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            return r.json().get("data", {}).get("providers", [])
        except Exception as e:
            logger.error(f"[GPU Scheduler] 获取 providers 失败: {e}")
            return None

    async def _patch_provider_enabled(self, provider_id: str, enabled: bool):
        """切换单个 provider 的启用状态"""
        api_url = self._cfg("api_url") or _CONFIG_DEFAULTS["api_url"]
        token = await self._get_token()
        r = await self._client.patch(
            f"{api_url.rstrip('/')}/api/v1/providers/{provider_id}/enabled",
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
        await asyncio.sleep(10)
        logger.info("[GPU Scheduler] 执行启动对齐...")

        now = datetime.datetime.now()
        target_mode = self._calc_target_mode(now)

        if target_mode == "deepseek":
            if self._mode != "deepseek":
                logger.info(f"[GPU Scheduler] 启动对齐: 当前={self._mode}, 目标=deepseek")
                await self.switch_to_deepseek()
            else:
                logger.info("[GPU Scheduler] 启动对齐: 已是 DeepSeek 模式，检查插件配置...")
                sync_result = await self._sync_plugin_providers_to_deepseek()
                logger.info(
                    f"[GPU Scheduler] 插件同步={sync_result.get('updated', 0)}/{sync_result.get('total', 0)}"
                )
        else:
            if self._mode != "ollama":
                logger.info(f"[GPU Scheduler] 启动对齐: 当前={self._mode}, 目标=ollama")
                await self.switch_to_ollama()
            else:
                logger.info("[GPU Scheduler] 启动对齐: 已是 Ollama 模式，检查插件配置...")
                sync_result = await self._restore_plugin_providers()
                logger.info(
                    f"[GPU Scheduler] 插件恢复={sync_result.get('restored', 0)}/{sync_result.get('total', 0)}"
                )

    def _calc_target_mode(self, now: datetime.datetime) -> str:
        """
        根据当前时间和配置计算目标模式。
        默认规则：
          - 周一至周四 18:00-次日 01:30: DeepSeek
          - 周五 18:00 - 周一 01:30: DeepSeek（周末全天，可关闭）
          - 其余时间: Ollama
        """
        weekday = now.weekday()  # 0=Mon, 6=Sun
        minutes = now.hour * 60 + now.minute

        # 工作日 DeepSeek 时间窗口
        wd_ds_h, wd_ds_m = self._parse_time(self._cfg("schedule_weekday_deepseek_time"))
        wd_ol_h, wd_ol_m = self._parse_time(self._cfg("schedule_weekday_ollama_time"))
        DS_START = wd_ds_h * 60 + wd_ds_m
        DS_END_NEXT_DAY = wd_ol_h * 60 + wd_ol_m

        enable_weekend = self._cfg("enable_weekend_deepseek")
        if enable_weekend:
            we_ds_h, we_ds_m = self._parse_time(self._cfg("schedule_weekend_deepseek_time"))
            we_ol_h, we_ol_m = self._parse_time(self._cfg("schedule_weekend_ollama_time"))
            WE_DS_START = we_ds_h * 60 + we_ds_m
            WE_DS_END = we_ol_h * 60 + we_ol_m

        # 周六、周日
        if weekday in (5, 6):
            return "deepseek" if enable_weekend else "ollama"

        # 周五
        if weekday == 4:
            if enable_weekend:
                if minutes >= WE_DS_START:
                    return "deepseek"
                if minutes <= DS_END_NEXT_DAY:
                    return "deepseek"  # 周四晚延续
                return "ollama"
            # 周末模式关闭 → 周五按工作日规则
            if minutes <= DS_END_NEXT_DAY:
                return "deepseek"
            if minutes >= DS_START:
                return "deepseek"
            return "ollama"

        # 周一
        if weekday == 0:
            if enable_weekend:
                if minutes <= WE_DS_END:
                    return "deepseek"  # 周末延续
                if minutes >= DS_START:
                    return "deepseek"
                return "ollama"
            if minutes <= DS_END_NEXT_DAY:
                return "deepseek"
            if minutes >= DS_START:
                return "deepseek"
            return "ollama"

        # 周二、周三、周四
        if minutes <= DS_END_NEXT_DAY:
            return "deepseek"
        if minutes >= DS_START:
            return "deepseek"
        return "ollama"

    # ============ 命令处理 ============

    @filter.command("gpu_free")
    async def cmd_gpu_free(self, event: AstrMessageEvent):
        """释放 GPU：切换至 DeepSeek"""
        sender_id = self._get_user_id(event)
        if sender_id not in self._get_allowed_users():
            yield event.plain_result("⛔ 无权限。请联系管理员将你加入 allowed_users。")
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
        if sender_id not in self._get_allowed_users():
            yield event.plain_result("⛔ 无权限。请联系管理员将你加入 allowed_users。")
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
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj:
                sender = getattr(msg_obj, "sender", None)
                if sender:
                    uid = getattr(sender, "user_id", None)
                    if uid:
                        return int(uid)
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
