from __future__ import annotations

import json
from typing import Any


def _claim_line(label: str, claim: dict[str, Any] | None) -> str:
    if not claim:
        return f"- **{label}**：未生成"
    value = claim.get("value")
    basis = claim.get("basis", "unknown")
    badge = {"observed": "观察", "inferred": "推断", "unknown": "未知"}.get(basis, basis)
    evidence = ", ".join(claim.get("evidence_frame_ids") or [])
    detail = f"（依据：{evidence}；{claim.get('note', '')}）" if evidence or claim.get("note") else ""
    return f"- **{label} · {badge}**：{value if value is not None else '无法判断'}{detail}"


def render_markdown(state: dict[str, Any], review: dict[str, Any] | None = None) -> str:
    lines = [
        "# 视频拆解 A/B 对照报告", "",
        f"- Comparison：`{state.get('comparison_id')}`",
        f"- Video：`{state.get('video_id')}`",
        f"- 状态：{state.get('status')}",
        f"- 模型请求名：`{state.get('model_id')}`",
        f"- 抽帧间隔：{state.get('interval_ms')} ms",
        f"- Prompt / Schema：`{state.get('prompt_version')}` / `{state.get('schema_version')}`",
        "- 模型只接收静态画面，没有分析音轨。", "",
    ]
    manifest = state.get("manifest") or {}
    lines += ["## 抽帧清单", "", f"共 {len(manifest.get('frames') or [])} 帧；视频时长 {manifest.get('duration_ms', '未知')} ms。", ""]
    for frame in manifest.get("frames") or []:
        lines.append(f"- {frame['frame_id']} · {frame['timestamp_ms']} ms · `{frame['image_sha256']}`")

    for branch_id, label in (("A", "方案 A · 单帧独立分析"), ("B", "方案 B · 有序多帧分析")):
        branch = (state.get("branches") or {}).get(branch_id) or {}
        lines += ["", f"## {label}", "", f"状态：{branch.get('status')}；耗时：{branch.get('wall_ms')} ms", ""]
        if branch.get("error"):
            lines.append(f"错误：{branch['error'].get('message', '未知错误')}")
        for error in branch.get("errors") or []:
            lines.append(f"- {error.get('frame_id')}: {error.get('message')}")
        segments = branch.get("temporal_segments") or []
        for index, segment in enumerate(segments):
            end_ms = segments[index + 1]["start_ms"] if index + 1 < len(segments) else segment.get("end_ms")
            lines += [
                "", f"### 连续片段 {index + 1:02d} · 约 {segment.get('start_ms')}–{end_ms} ms",
                "范围由采样帧分组估计，不是精确剪辑时间；末段仅覆盖到末采样帧。",
                f"帧范围：`{segment.get('start_frame_id')}` → `{segment.get('end_frame_id')}`",
                _claim_line("片段内容", segment.get("description")),
                _claim_line("动作发展", segment.get("action_progression")),
                _claim_line("运镜判断", segment.get("camera_motion")),
                _claim_line("镜头连续性", segment.get("continuity")),
            ]
        for frame in branch.get("frames") or []:
            lines += ["", f"### {frame.get('frame_id')} · {frame.get('timestamp_ms')} ms"]
            for key, title in (("subject", "主体"), ("scene", "场景"), ("shot_size", "景别"),
                               ("camera_angle", "角度"), ("composition", "构图"), ("lighting", "光线"),
                               ("color_palette", "色彩"), ("visible_pose", "可见姿态"), ("visible_text", "画面文字")):
                lines.append(_claim_line(title, frame.get(key)))
        report = branch.get("report") or {}
        if report:
            summary = report.get("summary") or {}
            lines += ["", "### 全片归纳"]
            for key, title in (("theme", "主题"), ("visual_style", "视觉风格"), ("content_structure", "内容结构")):
                lines.append(_claim_line(title, summary.get(key)))
            for claim in summary.get("reusable_features") or []:
                lines.append(_claim_line("可复用特征", claim))
            lines += ["", "### 区间变化"]
            for item in report.get("intervals") or []:
                lines += [f"", f"#### {item['from_frame_id']} → {item['to_frame_id']} · {item['start_ms']}–{item['end_ms']} ms"]
                for key, title in (("visual_change", "画面变化"), ("subject_motion", "主体运动"),
                                   ("camera_motion", "运镜"), ("transition", "转场")):
                    lines.append(_claim_line(title, item.get(key)))
            lines += ["", "### 参考视频提示词", "", report.get("source_prompt") or "未生成", ""]
            lines += ["### 品牌主题改编提示词", "", report.get("adapted_prompt") or "未填写目标主题，因此未生成。", ""]
            if report.get("adaptation_notes"):
                lines.append("改编说明：")
                lines.extend(f"- {note}" for note in report["adaptation_notes"])
            lines += ["", "### 限制", ""]
            lines.extend(f"- {item}" for item in report.get("limitations") or [])
        usage = branch.get("usage") or {}
        lines += ["", f"调用数：{len(branch.get('calls') or [])}；Token：{usage.get('total_tokens', 0)}（输入 {usage.get('prompt_tokens', 0)} / 输出 {usage.get('completion_tokens', 0)}）。"]

    if review:
        lines += ["", "## 业务评审", "", f"偏好：{review.get('preference', '未选择')}"]
        lines += [f"- {key}: {value}" for key, value in (review.get("scores") or {}).items() if value is not None]
        if review.get("evidence_note"):
            lines += ["", review["evidence_note"]]
    return "\n".join(lines).rstrip() + "\n"


def render_json(state: dict[str, Any], review: dict[str, Any] | None = None) -> bytes:
    payload = {"comparison": state, "review": review}
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
