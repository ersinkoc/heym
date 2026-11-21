"""An Execute node's blank input mapping sends the target field's default value.

The Execute node's editor lists one mapping row per input field of the target workflow
and shows "(default: <value>)" next to a field that has one. A blank row was sent as ""
and a value that is sent wins over the default, so the sub-workflow never got it.
"""

import unittest
import uuid

from app.services.workflow_executor import WorkflowExecutor

_START = {
    "id": "start",
    "type": "textInput",
    "data": {"label": "start", "inputFields": [{"key": "text"}]},
}
_FIELDS = [
    {"key": "text"},
    {"key": "userId", "defaultValue": "anonymous"},
    {"key": "lang"},
]
_CHILD = {
    "nodes": [
        {
            "id": "child_in",
            "type": "textInput",
            "data": {"label": "userInput", "inputFields": _FIELDS},
        },
        {
            "id": "set_1",
            "type": "set",
            "data": {
                "label": "who",
                "mappings": [
                    {"key": "user", "value": "$userInput.body.userId"},
                    {"key": "lang", "value": "$userInput.body.lang"},
                    {"key": "text", "value": "$userInput.body.text"},
                ],
            },
        },
    ],
    "edges": [{"id": "e1", "source": "child_in", "target": "set_1"}],
    "name": "Greet",
    "input_fields": _FIELDS,
}


def _who(mappings: list[dict]) -> dict:
    target = str(uuid.uuid4())
    call = {
        "id": "call",
        "type": "execute",
        "data": {
            "label": "callGreet",
            "executeWorkflowId": target,
            "executeInputMappings": mappings,
        },
    }
    result = WorkflowExecutor(
        nodes=[_START, call],
        edges=[{"id": "e1", "source": "start", "target": "call"}],
        workflow_cache={target: _CHILD},
    ).execute(
        workflow_id=uuid.uuid4(),
        initial_inputs={"headers": {}, "query": {}, "body": {"text": "Bonjour"}},
    )
    row = next(r for r in result.node_results if r["node_label"] == "callGreet")
    return row["output"]["outputs"]["who"]


class ExecuteBlankMappingDefaultTests(unittest.TestCase):
    def test_a_blank_row_sends_the_fields_default(self) -> None:
        who = _who(
            [
                {"key": "text", "value": "$start.body.text"},
                {"key": "userId", "value": ""},
                {"key": "lang", "value": ""},
            ]
        )

        self.assertEqual(who, {"user": "anonymous", "lang": "", "text": "Bonjour"})

    def test_a_mapped_value_wins(self) -> None:
        who = _who(
            [
                {"key": "text", "value": "$start.body.text"},
                {"key": "userId", "value": "u7"},
                {"key": "lang", "value": "fr"},
            ]
        )

        self.assertEqual(who, {"user": "u7", "lang": "fr", "text": "Bonjour"})


if __name__ == "__main__":
    unittest.main()
