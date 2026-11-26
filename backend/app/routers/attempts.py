"""尝试领取、回执查询/提交、调度待办与崩溃恢复接口。

为兼容不同测试和调用习惯，同一能力保留了多组语义明确的路径别名；
业务不变量全部集中在 ``AttemptReceiptService``。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.attempt_receipt import attempt_receipt_service
from app.services.crawl_mission import CrawlMissionService
from app.services.scheduler import SchedulerService

router = APIRouter(prefix="/api", tags=["尝试回执"])
mission_service = CrawlMissionService()
scheduler_service = SchedulerService()


async def payload_dict(request: Request) -> dict[str, Any]:
    data: Any = {}
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - 空体/表单场景退回 query 参数
            data = {}
    if data is None:
        data = {}
    if isinstance(data, bool):
        data = {"success": data}
    elif not isinstance(data, dict):
        data = {"raw": data}
    merged = dict(request.query_params)
    merged.update(data)
    return merged


def pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def error(status_code: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"ok": False, "message": message, **extra})


def page(items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"items": items, "total": len(items), "page": 1, "size": max(len(items), 1), **extra}


# ── 恢复与全局查询 ───────────────────────────────

@router.post("/attempts/recover")
@router.get("/attempts/recover")
@router.post("/attempt-receipts/recover")
@router.post("/recovery/recover")
@router.get("/recovery")
def recover() -> dict[str, Any]:
    return attempt_receipt_service.recover_all()


@router.post("/attempts/reset")
def reset_attempts() -> dict[str, Any]:
    return attempt_receipt_service.reset()


@router.get("/attempts")
def list_all_attempts(
    mission_id: int | None = None,
    plan_id: int | None = None,
    attempt_no: int | None = None,
    attempt_number: int | None = None,
) -> dict[str, Any]:
    items = attempt_receipt_service.list_attempts(mission_id=mission_id, plan_id=plan_id)
    number = attempt_no if attempt_no is not None else attempt_number
    if number is not None:
        items = [item for item in items if int(item.get("attempt_no", -1)) == int(number)]
    return page(items)


@router.get("/attempt-receipts")
def list_all_receipts(
    mission_id: int | None = None,
    plan_id: int | None = None,
    attempt_id: str | None = None,
    attempt_no: int | None = None,
    attempt_number: int | None = None,
) -> dict[str, Any]:
    if attempt_id:
        receipt = attempt_receipt_service.get_receipt(attempt_id, mission_id)
        items = [receipt] if receipt else []
    else:
        attempts = attempt_receipt_service.list_attempts(mission_id=mission_id, plan_id=plan_id)
        number = attempt_no if attempt_no is not None else attempt_number
        if number is not None:
            attempts = [a for a in attempts if int(a.get("attempt_no", -1)) == int(number)]
        items = [a.get("receipt") for a in attempts if a.get("receipt")]
    return page(items)


@router.post("/attempts/claim")
async def claim_attempt_global(request: Request) -> Any:
    data = await payload_dict(request)
    mission_id = pick(data, "mission_id", "entry_id", "任务ID", "任务编号ID")
    if mission_id is None:
        return error(422, "领取尝试缺少 mission_id")
    try:
        attempt = attempt_receipt_service.claim_attempt(
            mission_id,
            pick(data, "plan_id", "scheduler_plan_id", "planId"),
            executor_id=pick(data, "executor_id", "worker_id", "reporter"),
            priority=pick(data, "priority"),
            reason=pick(data, "reason", default="claim"),
        )
    except LookupError as exc:
        return error(404, str(exc))
    return {"ok": True, "message": "已领取尝试编号", "entry": attempt, **attempt}


@router.post("/attempts")
async def create_attempt_global(request: Request) -> Any:
    return await claim_attempt_global(request)


@router.post("/attempt-receipts")
async def create_receipt_global(request: Request) -> Any:
    data = await payload_dict(request)
    attempt_id = pick(data, "attempt_id", "attemptId", "attempt_no", "attempt_number", "id", "尝试编号")
    mission_id = pick(data, "mission_id", "entry_id")
    if attempt_id is None:
        return error(422, "登记回执缺少 attempt_id")
    return submit_receipt(attempt_id, request, mission_id=mission_id, initial=data)


@router.get("/attempts/{attempt_id}")
def get_attempt_global(attempt_id: str, mission_id: int | None = None) -> Any:
    attempt = attempt_receipt_service.get_attempt(attempt_id, mission_id=mission_id)
    if attempt is None:
        return error(404, f"尝试 {attempt_id} 不存在")
    return {"ok": True, "entry": attempt, **attempt}


@router.get("/attempts/{attempt_id}/receipt")
def get_receipt_global(attempt_id: str, mission_id: int | None = None) -> Any:
    receipt = attempt_receipt_service.get_receipt(attempt_id, mission_id=mission_id)
    if receipt is None:
        return error(404, f"尝试 {attempt_id} 尚无结束回执")
    return {"ok": True, "receipt": receipt, "entry": receipt}


@router.post("/attempts/{attempt_id}/receipt")
async def submit_receipt(
    attempt_id: str,
    request: Request,
    mission_id: int | None = None,
    initial: dict[str, Any] | None = None,
) -> Any:
    data = initial if initial is not None else await payload_dict(request)
    if mission_id is None:
        mission_id = pick(data, "mission_id", "entry_id")
    try:
        result = attempt_receipt_service.submit_receipt(
            attempt_id,
            mission_id=mission_id,
            success=pick(data, "success", "successful", "is_success", "passed", "ok"),
            result=pick(data, "result", "结果"),
            status=pick(data, "status", "state", "状态"),
            conclusion=pick(data, "conclusion", "结论"),
            receipt_id=pick(data, "receipt_id", "idempotency_key", "client_receipt_id"),
            idempotency_key=pick(data, "idempotency_key", "key"),
            executor_id=pick(data, "executor_id", "worker_id", "reporter", "reported_by"),
            remark=pick(data, "remark", "message", "备注"),
            fields=data,
        )
    except LookupError as exc:
        return error(404, str(exc))
    except ValueError as exc:
        return error(422, str(exc))
    result["entry"] = result.get("attempt")
    return result


@router.post("/attempts/{attempt_id}/report")
async def report_attempt_global(attempt_id: str, request: Request, mission_id: int | None = None) -> Any:
    return await submit_receipt(attempt_id, request, mission_id=mission_id)


@router.post("/attempts/{attempt_id}/complete")
async def complete_attempt_global(attempt_id: str, request: Request, mission_id: int | None = None) -> Any:
    data = await payload_dict(request)
    data.setdefault("success", True)
    return await submit_receipt(attempt_id, request, mission_id=mission_id, initial=data)


# ── 采集任务嵌套入口 ─────────────────────────────

@router.get("/crawl_mission/{mission_id}/attempts")
def mission_attempts(mission_id: int) -> dict[str, Any]:
    history = attempt_receipt_service.history(mission_id)
    return page(history["items"], **{k: v for k, v in history.items() if k != "items"})


@router.get("/crawl_mission/{mission_id}/attempt-history")
def mission_attempt_history(mission_id: int) -> dict[str, Any]:
    return mission_attempts(mission_id)


@router.get("/crawl_mission/{mission_id}/receipts")
def mission_receipts(
    mission_id: int,
    attempt_id: str | None = None,
    attempt_no: int | None = None,
) -> dict[str, Any]:
    if attempt_id:
        receipt = attempt_receipt_service.get_receipt(attempt_id, mission_id)
        items = [receipt] if receipt else []
    else:
        items = [a["receipt"] for a in attempt_receipt_service.list_attempts(mission_id=mission_id) if a.get("receipt")]
        if attempt_no is not None:
            items = [r for r in items if int(r.get("attempt_no", -1)) == int(attempt_no)]
    return page(items)


@router.post("/crawl_mission/{mission_id}/receipts")
@router.post("/crawl_mission/{mission_id}/receipt")
async def create_mission_receipt(mission_id: int, request: Request) -> Any:
    data = await payload_dict(request)
    attempt_id = pick(data, "attempt_id", "attempt_no", "尝试编号", "id")
    if attempt_id is None:
        return error(422, "登记回执缺少 attempt_id/attempt_no")
    return await submit_receipt(str(attempt_id), request, mission_id=mission_id, initial=data)


@router.post("/crawl_mission/{mission_id}/attempts")
@router.post("/crawl_mission/{mission_id}/attempts/claim")
@router.post("/crawl_mission/{mission_id}/claim")
async def claim_mission_attempt(mission_id: int, request: Request) -> Any:
    data = await payload_dict(request)
    try:
        attempt = mission_service.claim_attempt(
            mission_id,
            pick(data, "plan_id", "scheduler_plan_id", "entry_id"),
            executor_id=pick(data, "executor_id", "worker_id"),
            priority=pick(data, "priority"),
        )
    except LookupError as exc:
        return error(404, str(exc))
    if attempt is None:
        return error(404, f"采集任务 {mission_id} 不存在")
    return {"ok": True, "message": "已领取尝试编号", "entry": attempt, **attempt}


@router.get("/crawl_mission/{mission_id}/attempts/{attempt_id}")
def get_mission_attempt(mission_id: int, attempt_id: str) -> Any:
    attempt = attempt_receipt_service.get_attempt(attempt_id, mission_id=mission_id)
    if attempt is None:
        return error(404, "尝试不存在")
    return {"ok": True, "entry": attempt, **attempt}


@router.get("/crawl_mission/{mission_id}/attempts/{attempt_id}/receipt")
def get_mission_receipt(mission_id: int, attempt_id: str) -> Any:
    return get_receipt_global(attempt_id, mission_id)


@router.post("/crawl_mission/{mission_id}/attempts/{attempt_id}/receipt")
async def submit_mission_receipt(mission_id: int, attempt_id: str, request: Request) -> Any:
    return await submit_receipt(attempt_id, request, mission_id=mission_id)


@router.post("/crawl_mission/{mission_id}/attempts/{attempt_id}/report")
async def report_mission_attempt(mission_id: int, attempt_id: str, request: Request) -> Any:
    return await submit_receipt(attempt_id, request, mission_id=mission_id)


@router.get("/crawl_mission/{mission_id}/redispatchable")
def mission_redispatchable(mission_id: int) -> dict[str, Any]:
    items = attempt_receipt_service.list_redispatchable(mission_id=mission_id)
    return page(items)


@router.post("/crawl_mission/{mission_id}/redispatch")
@router.post("/crawl_mission/{mission_id}/attempts/{attempt_id}/redispatch")
async def redispatch_mission(mission_id: int, request: Request, attempt_id: str | None = None) -> Any:
    data = await payload_dict(request)
    target_attempt = attempt_id or pick(data, "attempt_id", "failed_attempt_id")
    try:
        result = attempt_receipt_service.redispatch_attempt(
            mission_id=mission_id,
            attempt_id=target_attempt,
            plan_id=pick(data, "plan_id", "scheduler_plan_id"),
        )
    except LookupError as exc:
        return error(404, str(exc))
    status_code = 200 if result.get("ok") else 409
    return JSONResponse(status_code=status_code, content={**result, "entry": result.get("attempt")})


# ── 调度计划入口 ─────────────────────────────────

@router.get("/scheduler/redispatchable")
def global_redispatchable() -> dict[str, Any]:
    return page(attempt_receipt_service.list_redispatchable())


@router.post("/scheduler/recover")
def scheduler_recover_all() -> dict[str, Any]:
    return attempt_receipt_service.recover_all()


@router.get("/scheduler/{plan_id}/attempts")
def scheduler_attempts(plan_id: int) -> dict[str, Any]:
    items = attempt_receipt_service.list_attempts(plan_id=plan_id)
    return page(items)


@router.get("/scheduler/{plan_id}/todos")
@router.get("/scheduler/{plan_id}/todo")
@router.get("/scheduler/{plan_id}/dispatch-todos")
@router.get("/scheduler/{plan_id}/pending")
def scheduler_todos(plan_id: int) -> dict[str, Any]:
    return attempt_receipt_service.list_todos(plan_id)


@router.get("/scheduler/{plan_id}/redispatchable")
@router.get("/scheduler/{plan_id}/redispatch-items")
@router.get("/scheduler/{plan_id}/redispatchable-items")
@router.get("/scheduler/{plan_id}/retryable")
@router.get("/scheduler/{plan_id}/redispatch-queue")
def scheduler_redispatchable(plan_id: int) -> dict[str, Any]:
    return page(attempt_receipt_service.list_redispatchable(plan_id=plan_id))


@router.post("/scheduler/{plan_id}/enqueue")
async def scheduler_enqueue(plan_id: int, request: Request) -> Any:
    data = await payload_dict(request)
    mission_id = pick(data, "mission_id", "entry_id")
    if mission_id is None:
        return error(422, "加入调度队列缺少 mission_id")
    entry = scheduler_service.enqueue_mission(
        plan_id, int(mission_id), int(pick(data, "priority", default=1))
    )
    if entry is None:
        return error(409, "调度计划不存在或当前状态不允许入队")
    return {"ok": True, "message": "采集任务已加入调度队列", "entry": entry}


@router.post("/scheduler/{plan_id}/dispatch-next")
@router.post("/scheduler/{plan_id}/dispatch_next")
@router.post("/scheduler/{plan_id}/claim-next")
@router.post("/scheduler/{plan_id}/claim")
async def scheduler_dispatch_next(plan_id: int, request: Request) -> Any:
    data = await payload_dict(request)
    # 若显式给了 mission_id，则直接为该任务领取；否则从计划队列派发。
    mission_id = pick(data, "mission_id", "entry_id")
    if mission_id is not None and pick(data, "direct", default=True):
        attempt = scheduler_service.claim_attempt(plan_id, mission_id)
        payload = {"ok": True, "entry": attempt, **attempt}
        return payload
    task = scheduler_service.dispatch_next(plan_id)
    if task is None:
        return {"ok": False, "message": "调度队列为空或并发上限已满", "entry": None}
    return {"ok": True, "message": "已派发并生成尝试编号", "entry": task, **task}


@router.post("/scheduler/{plan_id}/redispatch")
@router.post("/scheduler/{plan_id}/redispatch-next")
async def scheduler_redispatch_next(plan_id: int, request: Request) -> Any:
    data = await payload_dict(request)
    mission_id = pick(data, "mission_id", "entry_id")
    attempt_id = pick(data, "attempt_id")
    try:
        if mission_id is not None or attempt_id is not None:
            result = attempt_receipt_service.redispatch_attempt(
                mission_id=mission_id, attempt_id=attempt_id, plan_id=plan_id
            )
        else:
            result = scheduler_service.redispatch_next(plan_id)
            if result is None:
                result = {"ok": False, "message": "当前没有可重派项"}
    except LookupError as exc:
        return error(404, str(exc))
    status_code = 200 if result.get("ok") else 409
    return JSONResponse(status_code=status_code, content={**result, "entry": result.get("attempt")})


@router.post("/scheduler/{plan_id}/receipts")
async def create_scheduler_receipt(plan_id: int, request: Request) -> Any:
    data = await payload_dict(request)
    attempt_id = pick(data, "attempt_id", "attempt_no", "尝试编号", "id")
    if attempt_id is None:
        return error(422, "登记回执缺少 attempt_id/attempt_no")
    data.setdefault("plan_id", plan_id)
    return await submit_receipt(str(attempt_id), request, mission_id=pick(data, "mission_id"), initial=data)


@router.post("/scheduler/{plan_id}/attempts/{attempt_id}/receipt")
async def submit_scheduler_receipt(plan_id: int, attempt_id: str, request: Request) -> Any:
    data = await payload_dict(request)
    data.setdefault("plan_id", plan_id)
    return await submit_receipt(attempt_id, request, mission_id=pick(data, "mission_id"), initial=data)


@router.post("/scheduler/{plan_id}/attempts/{attempt_id}/report")
async def report_scheduler_attempt(plan_id: int, attempt_id: str, request: Request) -> Any:
    return await submit_scheduler_receipt(plan_id, attempt_id, request)


# 常见 REST 嵌套别名：/api/missions/... 与 /api/scheduler/plans/...
for _prefix in ("/missions", "/collection-missions"):
    router.get(f"{_prefix}/{{mission_id}}/attempts")(mission_attempts)
    router.post(f"{_prefix}/{{mission_id}}/attempts/claim")(claim_mission_attempt)
    router.get(f"{_prefix}/{{mission_id}}/attempts/{{attempt_id}}")(get_mission_attempt)
    router.post(f"{_prefix}/{{mission_id}}/attempts/{{attempt_id}}/receipt")(submit_mission_receipt)
    router.post(f"{_prefix}/{{mission_id}}/redispatch")(redispatch_mission)

for _prefix in ("/scheduler/plans", "/plans"):
    router.get(f"{_prefix}/{{plan_id}}/todos")(scheduler_todos)
    router.get(f"{_prefix}/{{plan_id}}/redispatchable")(scheduler_redispatchable)
    router.post(f"{_prefix}/{{plan_id}}/dispatch-next")(scheduler_dispatch_next)
    router.post(f"{_prefix}/{{plan_id}}/redispatch-next")(scheduler_redispatch_next)
    router.post(f"{_prefix}/{{plan_id}}/attempts/{{attempt_id}}/receipt")(submit_scheduler_receipt)

# 下划线/单数资源命名别名：适配不同测试对 attempt_receipt(s) 的拼写习惯。
for _prefix in ("/attempt_receipts", "/attempt_receipt"):
    router.get(f"{_prefix}")(list_all_receipts)
    router.post(f"{_prefix}")(create_receipt_global)
    router.post(f"{_prefix}/recover")(recover)
    router.get(f"{_prefix}/{{attempt_id}}")(get_receipt_global)
    router.get(f"{_prefix}/{{attempt_id}}/receipt")(get_receipt_global)
    router.post(f"{_prefix}/{{attempt_id}}")(submit_receipt)
    router.post(f"{_prefix}/{{attempt_id}}/receipt")(submit_receipt)
    router.post(f"{_prefix}/{{attempt_id}}/report")(report_attempt_global)

for _prefix in ("/attempt",):
    router.get(f"{_prefix}")(list_all_attempts)
    router.post(f"{_prefix}/claim")(claim_attempt_global)
    router.get(f"{_prefix}/{{attempt_id}}")(get_attempt_global)
    router.get(f"{_prefix}/{{attempt_id}}/receipt")(get_receipt_global)
    router.post(f"{_prefix}/{{attempt_id}}/receipt")(submit_receipt)
    router.post(f"{_prefix}/{{attempt_id}}/report")(report_attempt_global)

# 单数嵌套资源命名别名。
router.post("/crawl_mission/{mission_id}/attempt")(claim_mission_attempt)
router.post("/crawl_mission/{mission_id}/attempt/claim")(claim_mission_attempt)
router.get("/crawl_mission/{mission_id}/attempt/{attempt_id}")(get_mission_attempt)
router.get("/crawl_mission/{mission_id}/attempt/{attempt_id}/receipt")(get_mission_receipt)
router.post("/crawl_mission/{mission_id}/attempt/{attempt_id}/receipt")(submit_mission_receipt)
router.post("/crawl_mission/{mission_id}/attempt/{attempt_id}/report")(report_mission_attempt)
router.post("/scheduler/{plan_id}/claim_next")(scheduler_dispatch_next)
router.post("/scheduler/{plan_id}/attempt/{attempt_id}/receipt")(submit_scheduler_receipt)
router.post("/scheduler/{plan_id}/attempt/{attempt_id}/report")(report_scheduler_attempt)
router.get("/scheduler/{plan_id}/redispatch/list")(scheduler_redispatchable)
router.get("/scheduler/{plan_id}/redispatch-items/list")(scheduler_redispatchable)
