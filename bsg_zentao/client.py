"""
bsg_zentao/client.py

负责三件事：
  1. 从配置文件读取账号密码，登录禅道，拿到访问令牌
  2. 封装所有对禅道接口的请求（自动重试、自动解析 JSON）
  3. 提供短 TTL 缓存 + 离线降级，兼顾速度和实时性

缓存策略（v3）：
  - 在线：优先命中短 TTL 缓存（进程内 L1，其次本地磁盘 L2）
  - 在线且缓存过期：重新拉取禅道实时数据，并刷新缓存
  - 离线：自动降级读当天缓存，用户无感知
  - 无网络且无当天缓存：抛出明确错误提示用户
  - 支持 force_refresh=True 强制跳过缓存

外部使用方式：
    from bsg_zentao.client import ZentaoClient

    client = ZentaoClient()               # 自动读取配置、自动登录
    data   = client.fetch_versions()      # 取版本列表（undone）
    data   = client.fetch_pool(vid, pid)  # 取指定版本的需求池
    result = client.fetch_bugs(vid, pid)  # 取 Bug，返回 {bugs: [...], stat: {...}, deptReview: {...}}
"""

import json
import logging
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

import requests
import urllib3

# 关闭 SSL 警告（内网私有化部署，证书不受信任属正常情况）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

# ─── 固定常量 ─────────────────────────────────────────────────────────────────

BASE_URL    = "https://cd.baa360.cc:20088/index.php"
CONFIG_PATH = Path.home() / ".bsg-zentao" / "config.json"
CACHE_DIR   = Path(__file__).parent.parent / "缓存"  # bsg-zentao/缓存/，用户可直接查看

# 模拟浏览器请求头（禅道服务端会做校验）
_HEADERS = {
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "Accept-Encoding":  "gzip, deflate",
    "Accept-Language":  "zh-CN,zh;q=0.9",
    "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
}

_CACHE_SCHEMA = 1
_CACHE_TTL_DEFAULT = 60
_CACHE_TTLS = {
    "版本列表_": 120,
    "需求池_": 60,
    "Bug数据_": 30,
    "人员任务_": 60,
    "任务详情_": 60,
}


# ─── 配置加载 ─────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """
    从 ~/.bsg-zentao/config.json 读取账号密码。
    文件不存在时报错并提示用户先运行初始化命令。
    """
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"配置文件不存在：{CONFIG_PATH}\n"
            "请先运行初始化命令：python setup_config.py"
        )
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not config.get("account") or not config.get("password"):
        raise ValueError("配置文件缺少 account 或 password，请重新运行初始化命令。")
    return config


# ─── JSON 解析（带容错）────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    """
    解析禅道返回的 JSON 字符串，内置两步容错。

    禅道偶尔返回含非法控制字符或非法反斜杠转义的 JSON，直接解析会报错。
    第一步：清除非法控制字符（保留换行符和制表符）
    第二步：修复非法反斜杠转义序列（禅道的已知问题）
    """
    cleaned = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 修复非法反斜杠：合法转义有限，其余一律双倍反斜杠处理
    result, i = [], 0
    while i < len(cleaned):
        if cleaned[i] == "\\" and i + 1 < len(cleaned):
            if cleaned[i + 1] in '"\\/ bfnrtu':
                result.append(cleaned[i])
                result.append(cleaned[i + 1])
                i += 2
            else:
                result.append("\\\\")
                i += 1
        else:
            result.append(cleaned[i])
            i += 1
    return json.loads("".join(result))


# ─── 网络可用性检测 ──────────────────────────────────────────────────────────

def _is_network_available() -> bool:
    """快速探测禅道服务器是否可达（超时 3 秒）。"""
    try:
        requests.get(
            BASE_URL,
            params={"m": "user", "f": "login", "t": "json"},
            headers=_HEADERS,
            timeout=3,
            verify=False,
        )
        return True
    except requests.RequestException:
        return False


# ─── 缓存（离线降级专用）────────────────────────────────────────────────────

def _cache_path(name: str) -> Path:
    """生成缓存路径，格式：bsg-zentao/缓存/20260409_名称.json"""
    today = date.today().strftime("%Y%m%d")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{today}_{name}.json"


def _ttl_for_cache(name: str) -> int:
    for prefix, ttl in _CACHE_TTLS.items():
        if name.startswith(prefix):
            return ttl
    return _CACHE_TTL_DEFAULT


def _cache_envelope(name: str, data: dict | list) -> dict:
    return {
        "_meta": {
            "schema": _CACHE_SCHEMA,
            "name": name,
            "cached_at": time.time(),
        },
        "data": data,
    }


def _read_cache_entry(name: str) -> tuple[Optional[dict | list], Optional[float]]:
    path = _cache_path(name)
    if not path.exists():
        return None, None

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "_meta" in raw and "data" in raw:
        meta = raw.get("_meta") or {}
        cached_at = meta.get("cached_at")
        return raw["data"], float(cached_at) if cached_at else None

    # 兼容旧版只写原始数据的缓存文件
    return raw, None


def _load_cache(name: str, *, log_prefix: str) -> Optional[dict | list]:
    """读取当天缓存，不存在则返回 None。"""
    data, _ = _read_cache_entry(name)
    if data is not None:
        log.info("  %s%s", log_prefix, _cache_path(name).name)
    return data


def _save_cache(name: str, data: dict | list) -> None:
    """写入当天缓存（包含缓存时间元数据）。"""
    path = _cache_path(name)
    payload = _cache_envelope(name, data)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.debug("  已更新缓存：%s", path.name)


def _is_cache_fresh(cached_at: Optional[float], ttl_seconds: int) -> bool:
    if not cached_at:
        return False
    return (time.time() - cached_at) <= ttl_seconds


# ─── 主类 ─────────────────────────────────────────────────────────────────────

class ZentaoClient:
    """
    禅道接口客户端。
    实例化时自动读取配置并登录，后续所有请求自动携带令牌。
    """

    def __init__(self):
        config         = load_config()
        self._account  = config["account"]
        self._password = config["password"]
        self._online   = _is_network_available()
        self._memory_cache: dict[str, tuple[float, dict | list]] = {}
        if self._online:
            self._session = self._login()
        else:
            self._session = None
            log.warning("禅道网络不可达，将使用当天本地缓存（只读模式）")

    def _get_memory_cache(self, name: str, ttl_seconds: int) -> Optional[dict | list]:
        hit = self._memory_cache.get(name)
        if not hit:
            return None
        cached_at, data = hit
        if _is_cache_fresh(cached_at, ttl_seconds):
            log.info("  进程缓存命中：%s", name)
            return data
        self._memory_cache.pop(name, None)
        return None

    def _remember(self, name: str, data: dict | list) -> None:
        self._memory_cache[name] = (time.time(), data)

    def _load_fresh_disk_cache(self, name: str, ttl_seconds: int) -> Optional[dict | list]:
        data, cached_at = _read_cache_entry(name)
        if data is None or not _is_cache_fresh(cached_at, ttl_seconds):
            return None
        log.info("  短 TTL 磁盘缓存命中：%s", _cache_path(name).name)
        return data

    def _fetch_with_fallback(
        self,
        name: str,
        fetch_fn,
        *,
        ttl_seconds: int,
        force_refresh: bool = False,
    ) -> dict | list:
        """
        核心调度方法：优先短 TTL 缓存；无网络或实时请求失败时降级到当天磁盘缓存。

        name     : 缓存键名（用于读写缓存文件）
        fetch_fn : 无参可调用对象，执行实际网络请求并返回数据
        """
        if not force_refresh:
            data = self._get_memory_cache(name, ttl_seconds)
            if data is not None:
                return data

            if self._online:
                data = self._load_fresh_disk_cache(name, ttl_seconds)
                if data is not None:
                    self._remember(name, data)
                    return data

        if self._online:
            try:
                data = fetch_fn()
                self._remember(name, data)
                _save_cache(name, data)
                return data
            except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
                log.warning("网络请求失败，尝试降级到本地缓存：%s", e)
                cached = _load_cache(name, log_prefix="降级使用当天缓存：")
                if cached is not None:
                    self._remember(name, cached)
                    return cached
                raise RuntimeError(
                    f"禅道请求失败且无本地缓存可用（{name}）。\n"
                    f"请检查网络连接后重试。\n原始错误：{e}"
                ) from e
        else:
            # 离线模式：直接读缓存
            cached = _load_cache(name, log_prefix="离线降级：使用当天缓存 ")
            if cached is not None:
                self._remember(name, cached)
                return cached
            raise RuntimeError(
                f"当前无法访问禅道（网络不通），且本地没有今天的缓存数据（{name}）。\n"
                "请在能访问禅道内网时重新运行一次，系统会自动保存缓存供离线使用。"
            )

    # ── 登录 ─────────────────────────────────────────────────────────────────

    def _login(self) -> requests.Session:
        """
        登录禅道，返回已认证的 Session。

        登录三步（Skill 规范，已实测）：
          1. 发送账号密码，从响应中获取 token
          2. token 同时写入 URL 参数和 Cookie（两者缺一不可）
          3. 访问一次首页，让服务端建立完整 PHP Session
             跳过第 3 步会导致 pool/browse 等接口返回空数据
        """
        log.info("正在登录禅道…")
        session = requests.Session()
        session.verify = False  # 内网部署跳过 SSL 验证

        resp = session.get(
            BASE_URL,
            params={
                "m": "user", "f": "login",
                "account": self._account,
                "password": self._password,
                "t": "json",
            },
            headers=_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = _parse_json(resp.content.decode("utf-8", errors="replace"))

        if data.get("status") != "success":
            raise RuntimeError(
                f"登录失败，请检查账号密码是否正确。\n服务端返回：{data}"
            )

        token = data["user"]["token"]

        # token 必须同时设置在 URL 参数和 Cookie 中
        session.params = {"zentaosid": token}
        session.cookies.set("zentaosid", token, domain="cd.baa360.cc")

        # 建立 PHP Session（跳过此步会导致后续接口数据异常）
        session.get(
            BASE_URL,
            params={"m": "my", "f": "index"},
            headers=_HEADERS,
            timeout=15,
        )

        log.info("登录成功")
        return session

    # ── 基础请求（自动重试）───────────────────────────────────────────────────

    def _get(
        self,
        params: dict,
        label: str = "",
        extra_headers: Optional[dict] = None,
    ) -> dict:
        """
        发送 GET 请求并解析 JSON，失败自动重试最多 3 次。
        网络错误等 3 秒后重试，JSON 解析失败等 2 秒后重试。
        """
        headers = {**_HEADERS, **(extra_headers or {})}
        for attempt in range(1, 4):
            try:
                resp = self._session.get(
                    BASE_URL, params=params, headers=headers, timeout=30
                )
                resp.raise_for_status()
                raw = resp.content.decode("utf-8", errors="replace")
                if not raw.strip():
                    raise ValueError(f"接口返回了空响应（{label}）")
                return _parse_json(raw)

            except (ValueError, json.JSONDecodeError) as e:
                log.warning("JSON 解析失败（%s）：%s  [第 %d 次]", label, e, attempt)
                if attempt == 3:
                    raise
                time.sleep(2)

            except requests.RequestException as e:
                log.warning("请求失败（%s）：%s  [第 %d 次]", label, e, attempt)
                if attempt == 3:
                    raise
                time.sleep(3)

    # ── 接口1：版本列表（execution/all）─────────────────────────────────────

    def fetch_versions(self, status: str = "undone", force_refresh: bool = False) -> dict:
        """
        获取版本列表。

        status 参数说明（已实测，非推断）：
          "undone"  → 进行中和待开始的版本（日报/周报使用）
          "closed"  → 已关闭的历史版本（复盘使用，每次最多返回 100 条）
          ""/"done" → 返回空数组，禁止使用

        获取全量版本（含历史）= undone + closed 两次调用结果合并。
        接口不支持单次获取全量，pageID 参数对此接口无效。
        """
        if status not in ("undone", "closed"):
            raise ValueError(
                f"status 只能是 'undone' 或 'closed'，收到了：'{status}'\n"
                "空字符串和 'done' 会导致接口返回空数组。"
            )

        cache_name = f"版本列表_{status}"
        log.info("拉取版本列表（status=%s）…", status)

        def _fetch():
            return self._get(
                params={
                    "m": "execution", "f": "all",
                    "status": status, "orderBy": "order_asc",
                    "productID": "0", "getData": "1", "t": "html",
                },
                label="版本列表",
            )

        return self._fetch_with_fallback(
            cache_name, _fetch, ttl_seconds=_ttl_for_cache(cache_name), force_refresh=force_refresh
        )

    # ── 接口2：需求池（pool/browse）─────────────────────────────────────────

    def fetch_pool(self, version_id: str, project_id: str, force_refresh: bool = False) -> dict:
        """
        获取指定版本的需求池数据。

        接口参数说明（已实测确认）：
          timeType=timeType1       → 关键必须参数，缺少时只返回 1 条数据
          Referer                  → 脚本环境建议携带（浏览器环境下非必须，
                                     但脚本 session 机制不同，历史曾因缺失返回空）
          reviewPool/skins/deptCenter → 必须携带（传空字符串）

        返回数据关键字段：
          pools                    → 需求列表（含已取消的需求，调用方自行过滤）
          taskDetails              → 子任务明细，key=taskID，value={art/devel/cocos/web/qa/...}
          pms                      → PM 用户名→姓名映射
          users                    → 按部门分组的用户列表（用于 PHP1/PHP2 归属推断）
          associatedBugStat        → Bug 统计，结构：{taskID: {mainTaskCount, subTaskCount, total}}
          rejectedTaskStat         → 驳回次数，结构：{taskID: "驳回次数字符串"}
          statisticsReviewStory    → 需求评审统计：{unReview: N, pendingReview: N}
        """
        cache_name = f"需求池_{version_id}"
        log.info("拉取需求池（版本 ID=%s）…", version_id)
        referer = (
            f"{BASE_URL}?m=pool&f=browse"
            f"&version={version_id}&mode=3&projectSearch={project_id}"
        )

        def _fetch():
            return self._get(
                params={
                    "m": "pool", "f": "browse",
                    "version": version_id, "mode": "3",
                    "title": "", "category": "", "isShowMoreSearch": "0",
                    "pm": "", "tester": "0", "status": "", "phpGroup": "",
                    "pri": "", "desc": "",
                    "reviewPool": "", "skins": "", "deptCenter": "",
                    "timeType": "timeType1",
                    "begin": "", "end": "", "orderBy": "", "stateType": "",
                    "tag": "", "onlyWeeklyShow": "0",
                    "recTotal": "", "recPerPage": "200", "pageID": "1",
                    "projectSearch": project_id,
                    "t": "html", "getData": "1",
                },
                label=f"需求池_{version_id}",
                extra_headers={"Referer": referer},
            )

        return self._fetch_with_fallback(
            cache_name, _fetch, ttl_seconds=_ttl_for_cache(cache_name), force_refresh=force_refresh
        )

    # ── 接口3：Bug 数据（report/onlinebug）───────────────────────────────────

    def fetch_bugs(self, version_id: str, project_id: str, force_refresh: bool = False) -> dict:
        """
        获取指定版本的全量 Bug 数据，自动处理分页。

        返回结构（列表 + 统计 + 复盘 合并为一个对象，单个缓存文件）：
        {
            "bugs":       [...],   # 全量 Bug 对象列表
            "stat":       {...},   # 统计：count/activate/resolved/postponed/classification_N
            "deptReview": {...},   # 各部门复盘情况，key=Bug ID（Bug界定场景使用）
        }

        溯源相关字段（2026-04 实测）：
          phenomenon      → 现象
          scopeInfluence  → 影响范围
          demand          → 需求链接/说明
          useCase         → 用例链接
          disputeRemark   → 争议备注
          tracingBack     → 溯源说明（历史兼容文本块）
          exclusionReason → 剔除原因
        后续消费应优先读结构化字段；`tracingBack` 仅在缺失时作兼容回退。

        接口说明：
          mode=1  → 全量数据，统一使用此模式
          mode=2  → 已废弃，会遗漏数据，禁止使用

        mainTaskId 字段类型不一致（已实测）：
          第一条可能是 int(0)，后续是 string。
          统一处理方式：
            mid = b.get("mainTaskId", 0)
            task_id = str(mid) if mid and mid != 0 else None
        """
        cache_name = f"Bug数据_{version_id}"
        log.info("拉取 Bug 数据（版本 ID=%s）…", version_id)

        def _fetch():
            all_bugs    = []
            stat        = {}
            dept_review = {}
            page        = 1
            total       = None

            while True:
                data = self._get(
                    params={
                        "m": "report", "f": "onlinebug",
                        "version": version_id, "mode": "1",
                        "handleDept": "0", "dept": "0",
                        "questionType": "", "deptSearch": "", "scheduleStatus": "",
                        "openedBy": "", "deptOwner": "", "type": "",
                        "classification": "0", "isContainReanalyze": "0",
                        "qaConfirm": "", "title": "", "isShowMoreSearch": "0",
                        "recTotal": "", "recPerPage": "200", "pageID": str(page),
                        "ids": "", "projectSearch": project_id,
                        "belongSystem": "", "stateType": "", "severity": "0",
                        "t": "html", "getData": "1",
                    },
                    label=f"Bug数据_第{page}页",
                )

                bugs = data.get("onlinebug", [])
                all_bugs.extend(bugs)

                if page == 1:
                    stat        = data.get("stat", {})
                    dept_review = data.get("deptReview", {})
                    total       = int(stat.get("count", 0))

                log.info("  Bug 数据：已获取 %d / %d 条", len(all_bugs), total or "?")

                if not bugs or len(all_bugs) >= (total or 0):
                    break

                page += 1
                time.sleep(0.3)

            return {"bugs": all_bugs, "stat": stat, "deptReview": dept_review}

        return self._fetch_with_fallback(
            cache_name, _fetch, ttl_seconds=_ttl_for_cache(cache_name), force_refresh=force_refresh
        )

    # ── 接口4：人员任务汇总（report/workassignsummary）─────────────────────────

    def fetch_task_view(self, task_id: str, force_refresh: bool = False) -> dict:
        """
        获取单条任务详情（task/view）。

        返回结构保留禅道原始关键块：
        {
            "task":       {...},
            "actions":    {...},
            "subActions": {...},
            ...
        }

        说明：
          - `task.children` 中可读取子任务的 finishedDate / realStarted / openedDate 等时间字段
          - `actions` / `subActions` 可用于追踪开发完成、提测、自测等动作历史
        """
        cache_name = f"任务详情_{task_id}"
        log.info("拉取任务详情（taskID=%s）…", task_id)

        def _fetch():
            outer = self._get(
                params={
                    "m": "task", "f": "view",
                    "taskID": str(task_id),
                    "getData": "1", "t": "json",
                },
                label=f"任务详情_{task_id}",
            )
            data = outer.get("data")
            if isinstance(data, str):
                inner = _parse_json(data)
                if isinstance(inner, dict):
                    return inner
            return outer if isinstance(outer, dict) else {}

        return self._fetch_with_fallback(
            cache_name, _fetch, ttl_seconds=_ttl_for_cache(cache_name), force_refresh=force_refresh
        )

    def fetch_workassign(
        self,
        dept: str,
        project: str,
        begin: str,
        end: str,
        user: str = "",
        execution: str = "",
        show_done: int = 1,
        force_refresh: bool = False,
    ) -> dict:
        """
        获取指定部门的人员任务汇总。

        参数说明（已实测确认）：
          dept          → 部门 ID，如 "44"（PHP1部）
          project       → 项目 ID，平台项目="10"，游戏项目="51"
          begin/end     → 日期范围，格式 YYYY-MM-DD
          user          → 禁道账号，空 = 查该部门所有人，指定 = 只返回该用户数据
          execution     → 版本 ID，空 = 不限版本
          show_done     → 1=包含已完成任务，0=仅未完成

        返回数据关键字段：
          taskUsers     → {username: {count, estimate, consumed, done_estimate, done_consumed, task: [...]}}
          users         → 按组分类的用户列表，结构与 pool/browse 相同
          workTimes     → {username: {day_num: hours}}—按天工时
          noTaskUsers   → [username, ...]—本期无任务的人员
        """
        user_part = user if user else "all"
        cache_name = f"人员任务_{dept}_{user_part}_{begin}_{end}"
        log.info("拉取人员任务（dept=%s，user=%s，%s—%s）…", dept, user or "全部", begin, end)

        def _fetch():
            return self._get(
                params={
                    "m": "report", "f": "workassignsummary",
                    "dept": dept,
                    "user": user,
                    "begin": begin,
                    "end": end,
                    "project": project,
                    "execution": execution,
                    "showDone": str(show_done),
                    "onlyShowDone": "0",
                    "showResolveBug": "0",
                    "onlyShowResolveBug": "0",
                    "onlyShowMain": "0",
                    "isSelfTest": "",
                    "tabId": "taskTab",
                    "quickDate": "",
                    "functionId": "",
                    "group_id": "0",
                    "t": "html", "getData": "1",
                },
                label=f"人员任务_{dept}_{user_part}",
            )

        ttl_seconds = 60 if begin <= date.today().isoformat() <= end else 600
        return self._fetch_with_fallback(
            cache_name, _fetch, ttl_seconds=ttl_seconds, force_refresh=force_refresh
        )
