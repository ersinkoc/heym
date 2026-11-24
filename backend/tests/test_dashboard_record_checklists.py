"""A text widget's checkboxes on a detail page save for that record only (issue #1110).

The markdown is stored on the widget's Chart Output node under ``recordData[<record>]``.
The node shows it on that record's page; the dashboard and other records keep showing the
widget's own text. Only the markdown task endpoints write it: every other save keeps the
stored value, and graphs handed to a model leave it out.
"""

import asyncio
import copy
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import dashboards as dash_api
from app.api import workflows as workflows_api
from app.api.ai_assistant import AIAssistantRequest, _build_saved_workflow_payload
from app.config import settings
from app.db.models import Dashboard, DashboardWidget, User, Workflow, WorkflowVersion
from app.models.dashboard_schemas import MarkdownTaskToggleRequest, MarkdownTaskUpdateRequest
from app.models.schemas import RevertVersionRequest, WorkflowUpdate
from app.services.page_params import page_inputs
from app.services.workflow_executor import execute_workflow

CHECKLIST = "- [ ] Ship\n- [ ] Sail"


def _chart_payload(result) -> dict | None:
    for node_result in result.node_results:
        node_type = (
            node_result["node_type"] if isinstance(node_result, dict) else node_result.node_type
        )
        if node_type == "chartOutput":
            return node_result["output"] if isinstance(node_result, dict) else node_result.output
    return None


def _static_graph(chart_data: dict | None = None) -> list[dict]:
    data = {"label": "chart", "chartType": "text", "text": CHECKLIST, **(chart_data or {})}
    return [{"id": "chart", "type": "chartOutput", "position": {"x": 0, "y": 0}, "data": data}]


def _value_field_graph() -> tuple[list[dict], list[dict]]:
    nodes = [
        {
            "id": "src",
            "type": "set",
            "position": {"x": 0, "y": 0},
            "data": {"label": "data", "mappings": [{"key": "text", "value": CHECKLIST}]},
        },
        {
            "id": "chart",
            "type": "chartOutput",
            "position": {"x": 320, "y": 0},
            "data": {"label": "chart", "chartType": "text", "valueField": "text"},
        },
    ]
    return nodes, [{"id": "e1", "source": "src", "target": "chart"}]


class ChartOutputRecordTextTests(unittest.TestCase):
    """The node shows the text saved for the run's record, and nothing else changes."""

    def _run(self, nodes: list[dict], edges: list[dict], record: str | None) -> dict:
        result = execute_workflow(
            workflow_id=uuid.uuid4(), nodes=nodes, edges=edges, inputs=page_inputs(record)
        )
        payload = _chart_payload(result)
        self.assertIsNotNone(payload)
        return payload

    def test_a_record_with_an_entry_shows_its_saved_text(self) -> None:
        nodes = _static_graph({"recordData": {"ACME-1": {"text": "- [x] Ship\n- [ ] Sail"}}})

        payload = self._run(nodes, [], "ACME-1")

        self.assertEqual(payload["text"], "- [x] Ship\n- [ ] Sail")
        self.assertTrue(payload["text_interactive"])

    def test_the_saved_text_replaces_a_value_field_too(self) -> None:
        nodes, edges = _value_field_graph()
        nodes[1]["data"]["recordData"] = {"ACME-1": {"text": "- [x] Ship\n- [x] Sail"}}

        self.assertEqual(self._run(nodes, edges, "ACME-1")["text"], "- [x] Ship\n- [x] Sail")
        # Other records and the dashboard keep the computed markdown.
        self.assertEqual(self._run(nodes, edges, "ACME-2")["text"], CHECKLIST)
        self.assertEqual(self._run(nodes, edges, None)["text"], CHECKLIST)

    def test_only_text_charts_read_the_entries(self) -> None:
        nodes = _static_graph(
            {"chartType": "numeric", "recordData": {"ACME-1": {"text": "- [x] Ship"}}}
        )

        self.assertNotIn("text", self._run(nodes, [], "ACME-1"))


class RecordDataForModelsTests(unittest.TestCase):
    """Graphs sent to a model leave the per-record entries out."""

    def setUp(self) -> None:
        self.nodes = _static_graph({"recordData": {"ACME-1": {"text": "- [x] Ship"}}})

    def test_widget_fine_tune_snapshot(self) -> None:
        workflow = SimpleNamespace(name="Notes", description="", nodes=self.nodes, edges=[])

        snapshot = dash_api.widget_workflow_snapshot(workflow)

        self.assertNotIn("recordData", snapshot["nodes"][0]["data"])
        self.assertIn("recordData", self.nodes[0]["data"])

    def test_canvas_graph_sent_to_the_assistant(self) -> None:
        request = AIAssistantRequest(
            credential_id=uuid.uuid4(),
            model="m",
            message="Explain",
            current_workflow={"name": "Notes", "nodes": self.nodes, "edges": []},
        )

        self.assertNotIn("recordData", request.current_workflow["nodes"][0]["data"])
        self.assertEqual(request.current_workflow["nodes"][0]["data"]["text"], CHECKLIST)

    def test_saved_workflow_result_returned_to_the_model(self) -> None:
        workflow = SimpleNamespace(id=uuid.uuid4(), name="Notes", description="")

        result = _build_saved_workflow_payload(workflow, self.nodes, [], [], {}, status_value="ok")

        self.assertNotIn("recordData", result)


class RecordChecklistPostgresTests(unittest.IsolatedAsyncioTestCase):
    """Detail-page ticks through the dashboards API, with real widget runs."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            await self.engine.dispose()
            self.skipTest("PostgreSQL database is not reachable")
        self.owner = User(
            email=f"owner-{uuid.uuid4()}@example.com", hashed_password="x", name="Owner"
        )
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(self.owner)
            await db.flush()
            self.dashboard = Dashboard(owner_id=self.owner.id, name="Customer")
            self.workflow = Workflow(
                name="Notes",
                owner_id=self.owner.id,
                kind="dashboard_widget",
                nodes=_static_graph(),
                edges=[],
            )
            db.add_all([self.dashboard, self.workflow])
            await db.flush()
            self.widget = DashboardWidget(
                dashboard_id=self.dashboard.id,
                workflow_id=self.workflow.id,
                title="Notes",
                chart_type="text",
            )
            db.add(self.widget)
            await db.commit()
        self.patches = [
            patch.object(workflows_api, "announce_workflow_saved", AsyncMock()),
            patch.object(workflows_api, "audit"),
        ]
        for active in self.patches:
            active.start()

    async def asyncTearDown(self) -> None:
        for active in self.patches:
            active.stop()
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await db.execute(delete(Dashboard).where(Dashboard.id == self.dashboard.id))
            await db.execute(
                delete(WorkflowVersion).where(WorkflowVersion.workflow_id == self.workflow.id)
            )
            await db.execute(delete(Workflow).where(Workflow.id == self.workflow.id))
            await db.execute(delete(User).where(User.id == self.owner.id))
            await db.commit()
        await self.engine.dispose()

    async def _set_nodes(self, nodes: list[dict], edges: list[dict] | None = None) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            values: dict = {"nodes": nodes}
            if edges is not None:
                values["edges"] = edges
            await db.execute(update(Workflow).where(Workflow.id == self.workflow.id).values(values))
            await db.commit()

    async def _chart_data(self) -> dict:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            workflow = await db.get(Workflow, self.workflow.id)
            return next(n for n in workflow.nodes if n["type"] == "chartOutput")["data"]

    async def _shown(self, record: str | None) -> str:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            response = await dash_api.get_widget_data(
                self.widget.id, force=True, record=record, current_user=self.owner, db=db
            )
        self.assertIsNone(response.error)
        return response.payload["text"]

    async def _toggle(self, line_index: int, record: str | None) -> str:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            response = await dash_api.toggle_markdown_task(
                self.widget.id,
                MarkdownTaskToggleRequest(line_index=line_index, record=record),
                current_user=self.owner,
                db=db,
            )
        return response.payload["text"]

    async def test_a_record_tick_saves_for_that_record_only(self) -> None:
        shown = await self._toggle(0, "ACME-1")

        self.assertEqual(shown, "- [x] Ship\n- [ ] Sail")
        data = await self._chart_data()
        self.assertEqual(data["text"], CHECKLIST)
        self.assertEqual(data["recordData"], {"ACME-1": {"text": "- [x] Ship\n- [ ] Sail"}})
        self.assertEqual(await self._shown("ACME-1"), "- [x] Ship\n- [ ] Sail")
        self.assertEqual(await self._shown("ACME-2"), CHECKLIST)
        self.assertEqual(await self._shown(None), CHECKLIST)

    async def test_ticks_on_one_record_build_on_each_other(self) -> None:
        await self._toggle(0, "ACME-1")
        shown = await self._toggle(1, "ACME-1")

        self.assertEqual(shown, "- [x] Ship\n- [x] Sail")
        self.assertEqual(await self._shown("ACME-1"), "- [x] Ship\n- [x] Sail")
        self.assertEqual(await self._shown("ACME-2"), CHECKLIST)

    async def test_an_edit_saves_for_the_record_only(self) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            response = await dash_api.update_markdown_task(
                self.widget.id,
                MarkdownTaskUpdateRequest(line_index=1, text="Dock", record="ACME-1"),
                current_user=self.owner,
                db=db,
            )

        self.assertEqual(response.payload["text"], "- [ ] Ship\n- [ ] Dock")
        self.assertEqual((await self._chart_data())["text"], CHECKLIST)
        self.assertEqual(await self._shown("ACME-2"), CHECKLIST)

    async def test_a_value_field_widget_keeps_its_value_field(self) -> None:
        nodes, edges = _value_field_graph()
        await self._set_nodes(nodes, edges)

        shown = await self._toggle(1, "ACME-1")

        self.assertEqual(shown, "- [ ] Ship\n- [x] Sail")
        data = await self._chart_data()
        self.assertEqual(data["valueField"], "text")
        self.assertNotIn("text", data)
        self.assertEqual(await self._shown("ACME-2"), CHECKLIST)

    async def test_the_first_record_tick_starts_from_ticks_saved_in_text(self) -> None:
        await self._toggle(0, None)

        shown = await self._toggle(1, "ACME-1")

        self.assertEqual(shown, "- [x] Ship\n- [x] Sail")
        self.assertEqual(await self._shown(None), "- [x] Ship\n- [ ] Sail")

    async def test_a_dashboard_tick_keeps_the_record_entries(self) -> None:
        await self._toggle(0, "ACME-1")

        self.assertEqual(await self._toggle(1, None), "- [ ] Ship\n- [x] Sail")

        data = await self._chart_data()
        self.assertEqual(data["text"], "- [ ] Ship\n- [x] Sail")
        self.assertEqual(data["recordData"], {"ACME-1": {"text": "- [x] Ship\n- [ ] Sail"}})
        self.assertEqual(await self._shown("ACME-1"), "- [x] Ship\n- [ ] Sail")

    async def test_a_record_the_page_does_not_accept_is_refused(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self._toggle(0, "a b")

        self.assertEqual(raised.exception.status_code, 422)
        self.assertNotIn("recordData", await self._chart_data())

    async def test_a_tick_waits_for_a_tick_another_instance_is_writing(self) -> None:
        await self._shown("ACME-1")  # the page is open: its chart is cached
        async with AsyncSession(self.engine, expire_on_commit=False) as other:
            # Another instance holds the row while it saves a tick on the same record.
            await other.execute(
                select(Workflow.id).where(Workflow.id == self.workflow.id).with_for_update()
            )
            tick = asyncio.create_task(self._toggle(0, "ACME-1"))
            await self._wait_for_lock_wait(tick)
            await other.execute(
                update(Workflow)
                .where(Workflow.id == self.workflow.id)
                .values(
                    nodes=_static_graph(
                        {"recordData": {"ACME-1": {"text": "- [ ] Ship\n- [x] Sail"}}}
                    )
                )
            )
            await other.commit()
            shown = await tick

        self.assertEqual(shown, "- [x] Ship\n- [x] Sail")
        self.assertEqual(
            (await self._chart_data())["recordData"], {"ACME-1": {"text": "- [x] Ship\n- [x] Sail"}}
        )

    async def _wait_for_lock_wait(self, task: asyncio.Task) -> None:
        """Return once a connection waits for a row lock; fail if ``task`` ends first."""
        async with self.engine.connect() as conn:
            for _ in range(400):
                if task.done():
                    await task
                    self.fail("The tick finished without waiting for the row lock")
                waiting = await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
                if waiting:
                    return
                await asyncio.sleep(0.025)
        task.cancel()
        self.fail("The tick never waited for the row lock")

    async def test_canvas_saves_and_reverts_keep_the_record_entries(self) -> None:
        await self._toggle(0, "ACME-1")
        saved = {"ACME-1": {"text": "- [x] Ship\n- [ ] Sail"}}
        user = SimpleNamespace(id=self.owner.id, email=self.owner.email)

        # A canvas opened before the tick saves a new title without the entries.
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await workflows_api.update_workflow(
                self.workflow.id, WorkflowUpdate(nodes=_static_graph({"title": "Todo"})), user, db
            )
        self.assertEqual((await self._chart_data())["recordData"], saved)
        # A save cannot write entries of its own.
        forged = _static_graph({"title": "Todo", "recordData": {"ACME-9": {"text": "- [x] X"}}})
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await workflows_api.update_workflow(
                self.workflow.id, WorkflowUpdate(nodes=forged), user, db
            )
        self.assertEqual((await self._chart_data())["recordData"], saved)

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            version = (
                await db.execute(
                    select(WorkflowVersion).where(WorkflowVersion.workflow_id == self.workflow.id)
                )
            ).scalar_one()
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await workflows_api.revert_workflow_to_version(
                self.workflow.id, version.id, RevertVersionRequest(confirm=True), user, db
            )
        data = await self._chart_data()
        self.assertNotIn("title", data)
        self.assertEqual(data["recordData"], saved)

    async def test_widget_fine_tune_and_its_revert_keep_the_record_entries(self) -> None:
        await self._toggle(0, "ACME-1")
        saved = {"ACME-1": {"text": "- [x] Ship\n- [ ] Sail"}}
        refined = {"nodes": _static_graph({"title": "Todo"}), "edges": []}

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            widget = await db.get(DashboardWidget, self.widget.id)
            workflow = await db.get(Workflow, self.workflow.id)
            version_number = await dash_api.apply_refined_widget(
                db, widget, workflow, self.owner, copy.deepcopy(refined)
            )
        self.assertEqual((await self._chart_data())["title"], "Todo")
        self.assertEqual((await self._chart_data())["recordData"], saved)

        await self._toggle(1, "ACME-1")
        with patch.object(dash_api, "audit"):
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                await dash_api.revert_widget(
                    self.widget.id,
                    dash_api.WidgetRevertRequest(version_number=version_number),
                    current_user=self.owner,
                    db=db,
                )
        data = await self._chart_data()
        self.assertNotIn("title", data)
        self.assertEqual(data["recordData"], {"ACME-1": {"text": "- [x] Ship\n- [x] Sail"}})


if __name__ == "__main__":
    unittest.main()
