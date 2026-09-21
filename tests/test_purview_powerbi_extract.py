import unittest

from purview_powerbi_extract import (
    SchemaExtractor,
    classify_schema_entity,
    flatten_dataset,
    schema_relationship_names,
)


class FakeClient:
    def __init__(self):
        self.requested_guids = []
        self.entities = {
            "dataset-1": {
                "referredEntities": {
                    "table-1": {
                        "guid": "table-1",
                        "typeName": "powerbi_table",
                        "isIncomplete": False,
                        "attributes": {
                            "name": "pbi://sales/orders",
                            "displayName": "Orders",
                            "qualifiedName": "pbi://sales/orders",
                        },
                        "relationshipAttributes": {
                            "columns": [
                                {
                                    "guid": "column-1",
                                    "typeName": "powerbi_column",
                                }
                            ]
                        },
                    },
                    "column-1": {
                        "guid": "column-1",
                        "typeName": "powerbi_column",
                        "isIncomplete": False,
                        "attributes": {
                            "name": "OrderId",
                            "qualifiedName": "pbi://sales/orders/orderid",
                            "dataType": "Int64",
                        },
                        "relationshipAttributes": {},
                    },
                },
                "entity": {
                    "guid": "dataset-1",
                    "typeName": "powerbi_dataset",
                    "attributes": {
                        "name": "pbi://sales",
                        "displayName": "Sales",
                        "qualifiedName": "pbi://sales",
                    },
                    "relationshipAttributes": {
                        "tables": [
                            {
                                "guid": "table-1",
                                "typeName": "powerbi_table",
                            }
                        ],
                        "inputToProcesses": [{"guid": "process-1"}],
                    },
                }
            },
            "table-1": {
                "entity": {
                    "guid": "table-1",
                    "typeName": "powerbi_table",
                    "attributes": {
                        "name": "Orders",
                        "qualifiedName": "pbi://sales/orders",
                    },
                    "relationshipAttributes": {
                        "columns": [
                            {
                                "guid": "column-1",
                                "typeName": "powerbi_column",
                            }
                        ]
                    },
                }
            },
            "column-1": {
                "entity": {
                    "guid": "column-1",
                    "typeName": "powerbi_column",
                    "attributes": {
                        "name": "OrderId",
                        "qualifiedName": "pbi://sales/orders/orderid",
                        "dataType": "Int64",
                    },
                    "relationshipAttributes": {},
                }
            },
        }
        self.definitions = {
            "powerbi_dataset": {
                "name": "powerbi_dataset",
                "options": {"schemaElementsAttribute": "tables"},
            },
            "powerbi_table": {
                "name": "powerbi_table",
                "options": {"schemaElementsAttribute": "columns"},
            },
            "powerbi_column": {
                "name": "powerbi_column",
                "options": {},
            },
        }

    def get_type_definitions(self):
        return self.definitions

    def get_entity(self, guid):
        self.requested_guids.append(guid)
        return self.entities[guid]


class SchemaExtractionTests(unittest.TestCase):
    def test_schema_relationship_names_uses_type_option(self):
        entity = {"typeName": "custom_dataset"}
        definitions = {
            "custom_dataset": {
                "options": {"schemaElementsAttribute": "modelItems"}
            }
        }
        self.assertIn(
            "modelItems", schema_relationship_names(entity, definitions)
        )

    def test_entity_classification(self):
        self.assertEqual(
            "table", classify_schema_entity({"typeName": "pbi_table"}, "items")
        )
        self.assertEqual(
            "column",
            classify_schema_entity({"typeName": "pbi_field"}, "fields"),
        )

    def test_extracts_tables_and_columns_without_lineage(self):
        client = FakeClient()
        extractor = SchemaExtractor(client)
        dataset = extractor.extract_dataset("dataset-1")

        self.assertEqual(["dataset-1"], client.requested_guids)
        self.assertEqual("Sales", dataset["name"])
        self.assertEqual("dataset", dataset["kind"])
        self.assertEqual(["Orders"], [item["name"] for item in dataset["children"]])
        self.assertEqual(
            ["OrderId"],
            [item["name"] for item in dataset["children"][0]["children"]],
        )

        rows = list(flatten_dataset(dataset))
        self.assertEqual(2, len(rows))
        self.assertEqual("Orders", rows[1]["tableName"])
        self.assertEqual("Int64", rows[1]["dataType"])


if __name__ == "__main__":
    unittest.main()
