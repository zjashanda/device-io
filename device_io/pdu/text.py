"""Human-readable rendering for PDU documents (the ``--format text`` path).

JSON stays the contract; this module only decides how a status or plan reads in
a terminal. Chinese labels match the rest of device-io's text renderers.
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import contract


def _state_label(state: Any) -> str:
    return {"on": "通电", "off": "断电"}.get(str(state), "未知({0})".format(state))


def _number(value: Any, unit: str) -> str:
    if value is None:
        return "不可用"
    return "{0}{1}".format(value, unit)


def render_status(document: Dict[str, Any]) -> str:
    target = document.get("target") or {}
    lines: List[str] = [
        "PDU 状态（{0}）".format(document.get("schema")),
        "  生成时间：{0}".format(document.get("generated_at")),
        "  目标：{0}:{1}　传输：{2}".format(target.get("host"), target.get("port"), document.get("transport")),
        "  凭据来源：{0}（值已隐藏）".format((document.get("credential") or {}).get("source")),
    ]
    probe = document.get("http_probe") or {}
    if probe.get("performed"):
        lines.append("  预检 HTTP：{0}".format("可达" if probe.get("ok") else "不可达"))
    device = document.get("device") or {}
    if device.get("name"):
        lines.append("  设备名称：{0}".format(device["name"]))
    lines.append("")
    lines.append("插座（{0} 个）".format(len(document.get("outlets") or [])))
    for row in document.get("outlets") or []:
        if row.get("error"):
            lines.append(
                "  #{0}　读取失败：{1}".format(row.get("outlet"), row.get("error"))
            )
            continue
        lines.append(
            "  #{0}　{1}　{2}（读码 {3}）　电流 {4}".format(
                row.get("outlet"),
                row.get("name") or "-",
                _state_label(row.get("state")),
                row.get("read_code"),
                _number(row.get("current_a"), "A"),
            )
        )
    totals = document.get("totals") or {}
    if totals.get("available"):
        lines.append("")
        lines.append(
            "整机：电压 {0}　电流 {1}　功率 {2}　电量 {3}".format(
                _number(totals.get("voltage_v"), "V"),
                _number(totals.get("current_a"), "A"),
                _number(totals.get("power_w"), "W"),
                _number(totals.get("energy_kwh"), "kWh"),
            )
        )
    encoding = document.get("encoding") or {}
    if encoding:
        lines.append("")
        lines.append("编码约定：读 1=断电 2=通电 / 写 1=通电 2=断电（读写相反，禁止混用）")
    summary = document.get("summary") or {}
    if summary:
        lines.append(
            "汇总：通电 {0}　断电 {1}　未知 {2}".format(
                summary.get("on"), summary.get("off"), summary.get("unknown")
            )
        )
    return "\n".join(lines)


def render_plan(document: Dict[str, Any]) -> str:
    target = document.get("target") or {}
    mode = "执行并校验" if document.get("apply") else "预演（未写入）"
    lines: List[str] = [
        "PDU 操作：{0}（{1}）".format(document.get("action"), mode),
        "  生成时间：{0}".format(document.get("generated_at")),
        "  目标：{0}:{1}　插座 #{2}　传输：{3}".format(
            target.get("host"), target.get("port"), target.get("outlet"), document.get("transport")
        ),
    ]
    if document.get("before"):
        lines.append("  执行前：{0}（读码 {1}）".format(
            _state_label(document["before"].get("state")), document["before"].get("read_code")
        ))
    if document.get("current_state"):
        lines.append("  当前状态：{0}".format(document.get("current_state")))
    lines.append("")
    for step in document.get("steps") or []:
        if step.get("op") == "wait":
            lines.append("  {0}. 等待 {1}s（确保真正断电）".format(step.get("step"), step.get("seconds")))
            continue
        label = {"read": "读取", "set": "写入", "verify": "回读校验"}.get(step.get("op"), step.get("op"))
        detail = "{0} {1}".format(step.get("oid"), "" if step.get("value") is None else "= {0}".format(step.get("value")))
        lines.append(
            "  {0}. {1}　{2}　{3}".format(step.get("step"), label, detail.strip(), step.get("why") or "")
        )
    for phase in document.get("phases") or []:
        name = {"cut": "断电", "hold": "保持断电", "restore": "恢复通电"}.get(phase.get("phase"), phase.get("phase"))
        verdict = "已校验" if phase.get("verified") else "未校验"
        lines.append("  阶段 {0}：{1}".format(name, verdict))
        if phase.get("oid"):
            lines.append(
                "    写入 {0} = {1}（写码 {2}=关）　回读观测：{3}".format(
                    phase.get("oid"), phase.get("value"), phase.get("value"), phase.get("observed")
                )
            )
        if phase.get("seconds") is not None:
            lines.append("    保持 {0}s".format(phase.get("seconds")))
        for attempt in phase.get("restore_attempts") or []:
            lines.append(
                "    第 {0} 次恢复：写入 {1}={2}　校验 {3}　观测 {4}".format(
                    attempt.get("attempt"),
                    attempt.get("oid"),
                    attempt.get("value"),
                    "通过" if attempt.get("verified") else "失败",
                    attempt.get("observed"),
                )
            )
    if document.get("apply"):
        lines.append("")
        lines.append("结果：{0}".format("已回读校验通过" if document.get("verified") else "未通过校验"))
        if document.get("after"):
            lines.append("  执行后：{0}".format(_state_label(document["after"].get("state"))))
        if document.get("failure"):
            failure = document["failure"]
            lines.append("  失败阶段：{0}　原因：{1}".format(failure.get("phase"), failure.get("reason")))
            lines.append("  插座可能处于：{0}".format(failure.get("socket_may_be")))
            lines.append("  处置：{0}".format(failure.get("next")))
        if document.get("note"):
            lines.append("  说明：{0}".format(document["note"]))
    else:
        lines.append("")
        lines.append("未执行任何写入。加 --apply 才会下发；power-cycle 还需 --confirm-write。")
    return "\n".join(lines)


def render_doctor(document: Dict[str, Any]) -> str:
    lines: List[str] = [
        "PDU 适配器自检",
        "  生成时间：{0}".format(document.get("generated_at")),
        "  编码：读 1=断电 2=通电 / 写 1=通电 2=断电（读写相反，禁止混用）",
        "  凭据：{0}（{1}）".format(
            (document.get("credential") or {}).get("source"),
            (document.get("credential") or {}).get("detail"),
        ),
    ]
    for backend in document.get("backends") or []:
        lines.append(
            "  后端 {0}：{1}{2}".format(
                backend.get("backend"),
                "可用" if backend.get("available") else "不可用",
                "" if backend.get("available") else "（{0}）".format(backend.get("reason")),
            )
        )
    resolved = (document.get("oid_table") or {}).get("resolved")
    if resolved:
        lines.append(
            "  插座 #{0}：写 {1}　读 {2}".format(resolved.get("outlet"), resolved.get("switch_write"), resolved.get("state_read"))
        )
    for warning in document.get("warnings") or []:
        lines.append("  警告：{0}".format(warning))
    return "\n".join(lines)


def render_outlet(document: Dict[str, Any]) -> str:
    row = (document.get("outlets") or [{}])[0]
    lines = [
        "PDU 插座 #{0}（{1}）".format(row.get("outlet"), row.get("name") or "-"),
        "  状态：{0}　读码：{1}　电流：{2}".format(
            _state_label(row.get("state")), row.get("read_code"), _number(row.get("current_a"), "A")
        ),
        "  写码对照：通={0} 断={1}（与读码相反）".format(
            contract.write_code("on"), contract.write_code("off")
        ),
    ]
    if row.get("error"):
        lines.append("  错误：{0}".format(row.get("error")))
    return "\n".join(lines)


__all__ = ["render_doctor", "render_outlet", "render_plan", "render_status"]
