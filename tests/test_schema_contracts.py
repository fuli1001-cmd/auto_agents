import json
import unittest
from pathlib import Path


class TaskPlanSchemaTests(unittest.TestCase):
    def test_task_count_has_no_hard_maximum(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "task_plan.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        tasks_schema = schema["properties"]["tasks"]
        self.assertEqual(tasks_schema["minItems"], 1)
        self.assertNotIn("maxItems", tasks_schema)


if __name__ == "__main__":
    unittest.main()
