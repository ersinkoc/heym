from __future__ import annotations

from importlib import import_module

from app.services.chart_record_data import record_text
from app.services.node_execution.base import NodeExecutionContext
from app.services.node_execution.nodes.html_output_mapper_node import render_html_template


def execute(ctx: NodeExecutionContext) -> object:
    """Execute the chartOutput node."""
    _workflow_executor = import_module("app.services.workflow_executor")
    build_chart_payload = _workflow_executor.build_chart_payload
    self = ctx.executor
    inputs = ctx.inputs
    node_data = ctx.node_data

    visible_inputs = self._visible_inputs(inputs)
    if len(visible_inputs) == 1:
        source_data = next(iter(visible_inputs.values()))
    elif visible_inputs:
        merged: dict = {}
        for value in visible_inputs.values():
            if isinstance(value, dict):
                merged.update(value)
        source_data = merged
    else:
        source_data = {}
    page_params = getattr(self, "page_params", None)
    stored_text = record_text(
        node_data, page_params.get("record") if isinstance(page_params, dict) else None
    )
    if node_data.get("chartType") == "text" and stored_text is not None:
        # The checklist saved for this detail page's record replaces the computed markdown.
        node_data = {key: value for key, value in node_data.items() if key != "valueField"}
        node_data["text"] = stored_text
        source_data = {}
    if node_data.get("chartType") == "html":
        node_data = {**node_data, "html": render_html_template(ctx)}
    output = build_chart_payload(node_data, source_data)
    return output
