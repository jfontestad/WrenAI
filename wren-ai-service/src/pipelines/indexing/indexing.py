import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import orjson
from hamilton import base
from hamilton.experimental.h_async import AsyncDriver
from hamilton.function_modifiers import extract_fields
from haystack import Document, component
from haystack.components.writers import DocumentWriter
from haystack.document_stores.types import DocumentStore, DuplicatePolicy
from tqdm import tqdm

from src.core.pipeline import BasicPipeline, async_validate
from src.core.provider import DocumentStoreProvider, EmbedderProvider
from src.utils import async_timer, init_providers, timer

logger = logging.getLogger("wren-ai-service")

DATASET_NAME = os.getenv("DATASET_NAME")


@component
class DocumentCleaner:
    """
    This component is used to clear all the documents in the specified document store(s).

    """

    def __init__(self, stores: List[DocumentStore]) -> None:
        self._stores = stores

    @component.output_types(mdl=str)
    def run(self, mdl: str) -> str:
        def _clear_documents(store: DocumentStore) -> None:
            ids = [str(i) for i in range(store.count_documents())]
            if ids:
                store.delete_documents(ids)

        logger.info("Ask Indexing pipeline is clearing old documents...")
        [_clear_documents(store) for store in self._stores]
        return {"mdl": mdl}


@component
class MDLValidator:
    """
    Validate the MDL to check if it is a valid JSON and contains the required keys.
    """

    @component.output_types(mdl=Dict[str, Any])
    def run(self, mdl: str) -> str:
        try:
            mdl_json = orjson.loads(mdl)
            logger.debug(f"MDL JSON: {mdl_json}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")
        if "models" not in mdl_json:
            mdl_json["models"] = []
        if "views" not in mdl_json:
            mdl_json["views"] = []
        if "relationships" not in mdl_json:
            mdl_json["relationships"] = []
        if "metrics" not in mdl_json:
            mdl_json["metrics"] = []

        return {"mdl": mdl_json}


@component
class ViewConverter:
    """
    Convert the view MDL to the following format:
    {
      "question":"user original query",
      "summary":"the description generated by LLM",
      "statement":"the SQL statement generated by LLM",
      "viewId": "the view Id"
    }
    and store it in the view store.
    """

    @component.output_types(documents=List[Document])
    def run(self, mdl: Dict[str, Any]) -> None:
        def _format(view: Dict[str, Any]) -> List[str]:
            properties = view.get("properties", {})
            return str(
                {
                    "question": properties.get("question", ""),
                    "summary": properties.get("summary", ""),
                    "statement": view.get("statement", ""),
                    "viewId": properties.get("viewId", ""),
                }
            )

        converted_views = [_format(view) for view in mdl["views"]]

        return {
            "documents": [
                Document(
                    id=str(i),
                    meta={"id": str(i)},
                    content=converted_view,
                )
                for i, converted_view in enumerate(
                    tqdm(
                        converted_views,
                        desc="indexing view into the historical view question store",
                    )
                )
            ]
        }


@component
class DDLConverter:
    @component.output_types(documents=List[Document])
    def run(self, mdl: Dict[str, Any]):
        logger.info("Ask Indexing pipeline is writing new documents...")

        logger.debug(f"original mdl_json: {mdl}")

        semantics = {
            "models": [],
            "relationships": mdl["relationships"],
            "views": mdl["views"],
            "metrics": mdl["metrics"],
        }

        for model in mdl["models"]:
            columns = []
            for column in model["columns"]:
                ddl_column = {
                    "name": column["name"],
                    "type": column["type"],
                }
                if "properties" in column:
                    ddl_column["properties"] = column["properties"]
                if "relationship" in column:
                    ddl_column["relationship"] = column["relationship"]
                if "expression" in column:
                    ddl_column["expression"] = column["expression"]
                if "isCalculated" in column:
                    ddl_column["isCalculated"] = column["isCalculated"]

                columns.append(ddl_column)

            semantics["models"].append(
                {
                    "type": "model",
                    "name": model["name"],
                    "properties": model["properties"] if "properties" in model else {},
                    "columns": columns,
                    "primaryKey": model["primaryKey"],
                }
            )

        ddl_commands = (
            self._convert_models_and_relationships(
                semantics["models"], semantics["relationships"]
            )
            + self._convert_metrics(semantics["metrics"])
            + self._convert_views(semantics["views"])
        )

        return {
            "documents": [
                Document(
                    id=str(i),
                    meta={"id": str(i)},
                    content=ddl_command,
                )
                for i, ddl_command in enumerate(
                    tqdm(
                        ddl_commands,
                        desc="indexing ddl commands into the ddl store",
                    )
                )
            ]
        }

    # TODO: refactor this method
    def _convert_models_and_relationships(
        self, models: List[Dict[str, Any]], relationships: List[Dict[str, Any]]
    ) -> List[str]:
        ddl_commands = []

        # A map to store model primary keys for foreign key relationships
        primary_keys_map = {model["name"]: model["primaryKey"] for model in models}

        for model in models:
            table_name = model["name"]
            columns_ddl = []
            for column in model["columns"]:
                if "relationship" not in column:
                    if "properties" in column:
                        column["properties"]["alias"] = column["properties"].pop(
                            "displayName", ""
                        )
                        comment = f"-- {orjson.dumps(column['properties']).decode("utf-8")}\n  "
                    else:
                        comment = ""
                    if "isCalculated" in column and column["isCalculated"]:
                        comment = (
                            comment
                            + f"-- This column is a Calculated Field\n  -- column expression: {column["expression"]}\n  "
                        )
                    column_name = column["name"]
                    column_type = column["type"]
                    column_ddl = f"{comment}{column_name} {column_type}"

                    # If column is a primary key
                    if column_name == model.get("primaryKey", ""):
                        column_ddl += " PRIMARY KEY"

                    columns_ddl.append(column_ddl)

            # Add foreign key constraints based on relationships
            for relationship in relationships:
                comment = f'-- {{"condition": {relationship["condition"]}, "joinType": {relationship["joinType"]}}}\n  '
                if (
                    table_name == relationship["models"][0]
                    and relationship["joinType"].upper() == "MANY_TO_ONE"
                ):
                    related_table = relationship["models"][1]
                    fk_column = relationship["condition"].split(" = ")[0].split(".")[1]
                    fk_constraint = f"FOREIGN KEY ({fk_column}) REFERENCES {related_table}({primary_keys_map[related_table]})"
                    columns_ddl.append(f"{comment}{fk_constraint}")
                elif (
                    table_name == relationship["models"][1]
                    and relationship["joinType"].upper() == "ONE_TO_MANY"
                ):
                    related_table = relationship["models"][0]
                    fk_column = relationship["condition"].split(" = ")[1].split(".")[1]
                    fk_constraint = f"FOREIGN KEY ({fk_column}) REFERENCES {related_table}({primary_keys_map[related_table]})"
                    columns_ddl.append(f"{comment}{fk_constraint}")
                elif (
                    table_name in relationship["models"]
                    and relationship["joinType"].upper() == "ONE_TO_ONE"
                ):
                    index = relationship["models"].index(table_name)
                    related_table = [
                        m for m in relationship["models"] if m != table_name
                    ][0]
                    fk_column = (
                        relationship["condition"].split(" = ")[index].split(".")[1]
                    )
                    fk_constraint = f"FOREIGN KEY ({fk_column}) REFERENCES {related_table}({primary_keys_map[related_table]})"
                    columns_ddl.append(f"{comment}{fk_constraint}")

            if "properties" in model:
                model["properties"]["alias"] = model["properties"].pop(
                    "displayName", ""
                )
                comment = (
                    f"\n/* {orjson.dumps(model['properties']).decode("utf-8")} */\n"
                )
            else:
                comment = ""

            create_table_ddl = (
                f"{comment}CREATE TABLE {table_name} (\n  "
                + ",\n  ".join(columns_ddl)
                + "\n);"
            )
            ddl_commands.append(create_table_ddl)

        logger.debug(f"DDL Commands: {ddl_commands}")

        return ddl_commands

    def _convert_views(self, views: List[Dict[str, Any]]) -> List[str]:
        def _format(view: Dict[str, Any]) -> str:
            properties = view["properties"] if "properties" in view else ""
            return f"/* {properties} */\nCREATE VIEW {view['name']}\nAS ({view['statement']})"

        return [_format(view) for view in views]

    def _convert_metrics(self, metrics: List[Dict[str, Any]]) -> List[str]:
        ddl_commands = []

        for metric in metrics:
            table_name = metric["name"]
            columns_ddl = []
            for dimension in metric["dimension"]:
                column_name = dimension["name"]
                column_type = dimension["type"]
                comment = "-- This column is a dimension\n  "
                column_ddl = f"{comment}{column_name} {column_type}"
                columns_ddl.append(column_ddl)

            for measure in metric["measure"]:
                column_name = measure["name"]
                column_type = measure["type"]
                comment = f"-- This column is a measure\n  -- expression: {measure["expression"]}\n  "
                column_ddl = f"{comment}{column_name} {column_type}"
                columns_ddl.append(column_ddl)

            comment = f"\n/* This table is a metric */\n/* Metric Base Object: {metric["baseObject"]} */\n"
            create_table_ddl = (
                f"{comment}CREATE TABLE {table_name} (\n  "
                + ",\n  ".join(columns_ddl)
                + "\n);"
            )

            ddl_commands.append(create_table_ddl)

        return ddl_commands


## Start of Pipeline
@timer
def clean_document_store(mdl_str: str, cleaner: DocumentCleaner) -> Dict[str, Any]:
    logger.debug(f"input in clean_document_store: {mdl_str}")
    return cleaner.run(mdl=mdl_str)


@timer
@extract_fields(dict(mdl=Dict[str, Any]))
def validate_mdl(
    clean_document_store: Dict[str, Any], validator: MDLValidator
) -> Dict[str, Any]:
    logger.debug(f"input in validate_mdl: {clean_document_store}")
    mdl = clean_document_store.get("mdl")
    res = validator.run(mdl=mdl)
    return dict(mdl=res["mdl"])


@timer
def convert_to_ddl(mdl: Dict[str, Any], ddl_converter: DDLConverter) -> Dict[str, Any]:
    logger.debug(f"input in convert_to_ddl: {mdl}")
    return ddl_converter.run(mdl=mdl)


@async_timer
async def embed_ddl(
    convert_to_ddl: Dict[str, Any], ddl_embedder: Any
) -> Dict[str, Any]:
    logger.debug(f"input in embed_ddl: {convert_to_ddl}")
    return await ddl_embedder.run(documents=convert_to_ddl["documents"])


@timer
def write_ddl(embed_ddl: Dict[str, Any], ddl_writer: DocumentWriter) -> None:
    logger.debug(f"input in write_ddl: {embed_ddl}")
    return ddl_writer.run(documents=embed_ddl["documents"])


@timer
def convert_to_view(
    mdl: Dict[str, Any], view_converter: ViewConverter
) -> Dict[str, Any]:
    logger.debug(f"input in convert_to_view: {mdl}")
    return view_converter.run(mdl=mdl)


@async_timer
async def embed_view(
    convert_to_view: Dict[str, Any], view_embedder: Any
) -> Dict[str, Any]:
    logger.debug(f"input in embed_view: {convert_to_view}")
    return await view_embedder.run(documents=convert_to_view["documents"])


@timer
def write_view(embed_view: Dict[str, Any], view_writer: DocumentWriter) -> None:
    logger.debug(f"input in write_view: {embed_view}")
    return view_writer.run(documents=embed_view["documents"])


## End of Pipeline


class Indexing(BasicPipeline):
    def __init__(
        self,
        embedder_provider: EmbedderProvider,
        document_store_provider: DocumentStoreProvider,
    ) -> None:
        ddl_store = document_store_provider.get_store()
        view_store = document_store_provider.get_store(dataset_name="view_questions")

        self.cleaner = DocumentCleaner([ddl_store, view_store])
        self.validator = MDLValidator()

        self.ddl_converter = DDLConverter()
        self.ddl_embedder = embedder_provider.get_document_embedder()
        self.ddl_writer = DocumentWriter(
            document_store=ddl_store,
            policy=DuplicatePolicy.OVERWRITE,
        )
        self.view_converter = ViewConverter()
        self.view_embedder = embedder_provider.get_document_embedder()
        self.view_writer = DocumentWriter(
            document_store=view_store,
            policy=DuplicatePolicy.OVERWRITE,
        )

        super().__init__(
            AsyncDriver({}, sys.modules[__name__], result_builder=base.DictResult())
        )

    def visualize(self, mdl_str: str) -> None:
        destination = "outputs/pipelines/indexing"
        if not Path(destination).exists():
            Path(destination).mkdir(parents=True, exist_ok=True)

        self._pipe.visualize_execution(
            ["write_ddl", "write_view"],
            output_file_path=f"{destination}/indexing.dot",
            inputs={
                "mdl_str": mdl_str,
                "cleaner": self.cleaner,
                "validator": self.validator,
                "ddl_converter": self.ddl_converter,
                "ddl_embedder": self.ddl_embedder,
                "ddl_writer": self.ddl_writer,
                "view_converter": self.view_converter,
                "view_embedder": self.view_embedder,
                "view_writer": self.view_writer,
            },
            show_legend=True,
            orient="LR",
        )

    @async_timer
    async def run(self, mdl_str: str) -> Dict[str, Any]:
        logger.info("Ask Indexing pipeline is running...")
        return await self._pipe.execute(
            ["write_ddl", "write_view"],
            inputs={
                "mdl_str": mdl_str,
                "cleaner": self.cleaner,
                "validator": self.validator,
                "ddl_converter": self.ddl_converter,
                "ddl_embedder": self.ddl_embedder,
                "ddl_writer": self.ddl_writer,
                "view_converter": self.view_converter,
                "view_embedder": self.view_embedder,
                "view_writer": self.view_writer,
            },
        )


if __name__ == "__main__":
    from src.utils import load_env_vars

    load_env_vars()
    _, embedder_provider, document_store_provider, _ = init_providers()

    pipeline = Indexing(
        embedder_provider=embedder_provider,
        document_store_provider=document_store_provider,
    )

    input = '{"models": [], "views": [], "relationships": [], "metrics": []}'
    pipeline.visualize(input)
    async_validate(lambda: pipeline.run(input))
