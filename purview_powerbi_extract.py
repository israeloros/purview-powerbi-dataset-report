#!/usr/bin/env python3
"""Export Power BI semantic-model metadata from Microsoft Purview Data Map.

The program performs four high-level operations:

1. Load the Purview endpoint and authentication settings from command-line
   arguments and an optional ``.env`` file.
2. Authenticate with either a service principal or Azure Identity's default
   credential chain.
3. Search the Data Map for Power BI datasets and traverse their Atlas
   relationships to schemas, tables, columns, and similar schema elements.
4. Write the discovered hierarchy to JSON and a normalized representation to
   CSV.

Power BI entity type and relationship names can vary between Purview versions.
The extractor therefore reads Atlas type definitions at runtime and combines
their metadata with conservative naming heuristics.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
from azure.core.credentials import TokenCredential
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


TOKEN_SCOPE = "https://purview.azure.net/.default"
DEFAULT_API_VERSION = "2023-09-01"
DEFAULT_DATASET_TYPES = (
    "powerbi_dataset",
    "azure_powerbi_dataset",
    "azure_pbi_dataset",
)
SCHEMA_RELATIONSHIP_NAMES = {
    "schema",
    "schemas",
    "tabular_schema",
    "tables",
    "columns",
    "fields",
}

LOGGER = logging.getLogger("purview-powerbi-extract")


class PurviewApiError(RuntimeError):
    """Raised when a Purview Data Map API request fails."""


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration and optional service-principal values."""

    endpoint: str
    api_version: str
    auth_mode: str
    tenant_id: str | None
    client_id: str | None
    client_secret: str | None


def first_environment_value(*names: str) -> str | None:
    """Return the first non-empty environment variable from ``names``.

    Purview-specific variable names are passed before standard Azure Identity
    names so callers can deliberately override shared Azure configuration.
    """

    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return None


def normalize_endpoint(value: str) -> str:
    """Normalize a Purview endpoint by adding HTTPS and removing a trailing slash."""

    endpoint = value.strip().rstrip("/")
    if not endpoint.startswith(("https://", "http://")):
        endpoint = f"https://{endpoint}"
    return endpoint


def load_settings(args: argparse.Namespace) -> Settings:
    """Load environment values and merge them with command-line arguments.

    Explicit command-line endpoint values take precedence. The ``.env`` loader
    does not overwrite variables already present in the process environment.
    """

    if args.env_file:
        load_dotenv(args.env_file, override=False)
    else:
        load_dotenv(override=False)

    endpoint_value = args.endpoint or first_environment_value(
        "PURVIEW_ENDPOINT", "PURVIEW_ATLAS_ENDPOINT"
    )
    account_name = args.account_name or first_environment_value(
        "PURVIEW_ACCOUNT_NAME"
    )
    if not endpoint_value and account_name:
        endpoint_value = f"https://{account_name}.purview.azure.com"
    if not endpoint_value:
        raise ValueError(
            "Set PURVIEW_ENDPOINT or PURVIEW_ACCOUNT_NAME, or pass --endpoint/--account-name."
        )

    return Settings(
        endpoint=normalize_endpoint(endpoint_value),
        api_version=args.api_version,
        auth_mode=args.auth,
        tenant_id=first_environment_value("PURVIEW_TENANT_ID", "AZURE_TENANT_ID"),
        client_id=first_environment_value("PURVIEW_CLIENT_ID", "AZURE_CLIENT_ID"),
        client_secret=first_environment_value(
            "PURVIEW_CLIENT_SECRET", "AZURE_CLIENT_SECRET"
        ),
    )


def create_credential(settings: Settings) -> TokenCredential:
    """Create the Azure credential selected by the configured authentication mode.

    ``service-principal`` requires all three client-secret values. ``azure``
    always uses :class:`DefaultAzureCredential`. ``auto`` prefers an explicitly
    configured service principal and otherwise falls back to the Azure
    credential chain.
    """

    has_service_principal = all(
        (settings.tenant_id, settings.client_id, settings.client_secret)
    )
    if settings.auth_mode == "service-principal" or (
        settings.auth_mode == "auto" and has_service_principal
    ):
        if not has_service_principal:
            raise ValueError(
                "Service-principal authentication requires tenant ID, client ID, "
                "and client secret in the environment file."
            )
        return ClientSecretCredential(
            tenant_id=settings.tenant_id,
            client_id=settings.client_id,
            client_secret=settings.client_secret,
        )

    return DefaultAzureCredential(
        exclude_interactive_browser_credential=not bool(
            os.getenv("PURVIEW_ALLOW_INTERACTIVE_BROWSER")
        )
    )


class PurviewAtlasClient:
    """Small HTTP client for the Purview Data Map search and Atlas APIs."""

    def __init__(
        self,
        endpoint: str,
        credential: TokenCredential,
        api_version: str = DEFAULT_API_VERSION,
        timeout: int = 60,
    ) -> None:
        """Initialize a reusable HTTP session with transient-failure retries."""

        self.endpoint = endpoint.rstrip("/")
        self.credential = credential
        self.api_version = api_version
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self._type_definitions: dict[str, dict[str, Any]] | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an authenticated request and return its decoded JSON object.

        A token is requested for every call. Azure Identity caches valid access
        tokens internally, so this keeps token refresh handling centralized
        without forcing a network token request for every API operation.

        Raises:
            PurviewApiError: If Purview returns a non-success HTTP status.
            requests.RequestException: If the HTTP request itself fails.
        """

        token = self.credential.get_token(TOKEN_SCOPE).token
        response = self.session.request(
            method,
            f"{self.endpoint}{path}",
            params=params,
            json=json_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        if not response.ok:
            request_id = response.headers.get("x-ms-request-id", "not supplied")
            try:
                details = response.json()
            except ValueError:
                details = response.text[:1000]
            raise PurviewApiError(
                f"{method} {response.url} failed with HTTP {response.status_code}; "
                f"request ID: {request_id}; response: {details}"
            )
        if not response.content:
            return {}
        return response.json()

    def get_type_definitions(self) -> dict[str, dict[str, Any]]:
        """Return Atlas entity definitions indexed by type name.

        Type definitions are cached because they are static for the duration of
        an extraction and are consulted repeatedly during relationship
        traversal.
        """

        if self._type_definitions is None:
            response = self.request(
                "GET",
                "/datamap/api/atlas/v2/types/typedefs",
                params={"api-version": self.api_version},
            )
            self._type_definitions = {
                definition["name"]: definition
                for definition in response.get("entityDefs", [])
                if definition.get("name")
            }
        return self._type_definitions

    def find_dataset_types(self, requested_types: list[str] | None) -> list[str]:
        """Resolve Power BI dataset types from user input or Atlas definitions.

        Automatic discovery searches names, display names, and descriptions for
        both a Power BI marker and a dataset/semantic-model marker. Known type
        names are used only as a fallback.
        """

        definitions = self.get_type_definitions()
        if requested_types:
            missing = [name for name in requested_types if name not in definitions]
            if missing:
                LOGGER.warning(
                    "Requested type definitions were not returned by Purview: %s",
                    ", ".join(missing),
                )
            return requested_types

        discovered = []
        for name, definition in definitions.items():
            searchable = " ".join(
                (
                    name,
                    str(definition.get("displayName", "")),
                    str(definition.get("description", "")),
                )
            ).lower()
            is_power_bi = (
                "powerbi" in searchable
                or "power bi" in searchable
                or "pbi_" in name.lower()
            )
            is_dataset = (
                "dataset" in searchable
                or "semantic model" in searchable
                or "semantic_model" in searchable
            )
            if is_power_bi and is_dataset:
                discovered.append(name)

        if discovered:
            return sorted(set(discovered))

        fallback = [name for name in DEFAULT_DATASET_TYPES if name in definitions]
        if fallback:
            return fallback
        raise PurviewApiError(
            "No Power BI dataset entity type was found. Use --dataset-type with the "
            "entity type shown in your Purview type definitions."
        )

    def search_entities(
        self, entity_type: str, page_size: int = 1000
    ) -> Iterable[dict[str, Any]]:
        """Yield all active search results for an Atlas entity type.

        Purview returns a continuation token when more results are available.
        This generator follows that token until the final page.
        """

        continuation_token: str | None = None
        while True:
            body: dict[str, Any] = {
                "keywords": None,
                "filter": {"entityType": entity_type},
                "limit": page_size,
            }
            if continuation_token:
                body["continuationToken"] = continuation_token
            response = self.request(
                "POST",
                "/datamap/api/search/query",
                params={"api-version": self.api_version},
                json_body=body,
            )
            yield from response.get("value", [])
            continuation_token = response.get("continuationToken")
            if not continuation_token:
                break

    def get_entity(self, guid: str) -> dict[str, Any]:
        """Retrieve an entity, its relationships, and referred entities by GUID."""

        return self.request(
            "GET",
            f"/datamap/api/atlas/v2/entity/guid/{quote(guid, safe='')}",
            params={
                "api-version": self.api_version,
                "minExtInfo": "false",
                "ignoreRelationships": "false",
            },
        )


def entity_summary(entity: dict[str, Any]) -> dict[str, Any]:
    """Convert a full Atlas entity into the stable shape used in output files.

    ``displayName`` is preferred because some Power BI entities store a URL in
    ``name`` while exposing the human-readable asset name separately.
    """

    attributes = entity.get("attributes") or {}
    excluded = {"name", "qualifiedName"}
    return {
        "guid": entity.get("guid"),
        "typeName": entity.get("typeName"),
        "name": (
            attributes.get("displayName")
            or entity.get("displayText")
            or attributes.get("name")
        ),
        "qualifiedName": attributes.get("qualifiedName"),
        "status": entity.get("status"),
        "attributes": {
            key: value for key, value in attributes.items() if key not in excluded
        },
        "classifications": entity.get("classifications", []),
        "labels": entity.get("labels", []),
        "contacts": entity.get("contacts", {}),
    }


def iter_entity_references(value: Any) -> Iterable[dict[str, Any]]:
    """Recursively yield GUID-bearing entity references from relationship data."""

    if isinstance(value, list):
        for item in value:
            yield from iter_entity_references(item)
    elif isinstance(value, dict):
        if value.get("guid"):
            yield value
        else:
            for nested in value.values():
                yield from iter_entity_references(nested)


def schema_relationship_names(
    entity: dict[str, Any], type_definitions: dict[str, dict[str, Any]]
) -> set[str]:
    """Determine which relationships may contain schema elements.

    The Atlas ``schemaElementsAttribute`` option is authoritative when present.
    Common built-in names and schema-like relationship definitions provide
    compatibility with Power BI and custom entity types that omit the option.
    """

    names = set(SCHEMA_RELATIONSHIP_NAMES)
    definition = type_definitions.get(entity.get("typeName"), {})
    options = definition.get("options") or {}
    schema_elements = options.get("schemaElementsAttribute")
    if isinstance(schema_elements, str):
        names.add(schema_elements)

    for attribute in definition.get("relationshipAttributeDefs", []):
        name = attribute.get("name")
        if name and any(
            marker in name.lower()
            for marker in ("schema", "table", "column", "field")
        ):
            names.add(name)
    return names


def classify_schema_entity(entity: dict[str, Any], relationship_name: str) -> str:
    """Classify an entity as a schema, table, column, or generic schema element."""

    type_name = str(entity.get("typeName", "")).lower()
    relationship_name = relationship_name.lower()
    if "column" in type_name or "field" in type_name or relationship_name in {
        "columns",
        "fields",
    }:
        return "column"
    if "table" in type_name or relationship_name == "tables":
        return "table"
    if "schema" in type_name or relationship_name in {
        "schema",
        "schemas",
        "tabular_schema",
    }:
        return "schema"
    return "schemaElement"


class SchemaExtractor:
    """Build hierarchical dataset metadata by traversing Atlas relationships."""

    def __init__(self, client: PurviewAtlasClient) -> None:
        """Initialize traversal state and load the account's type definitions."""

        self.client = client
        self.type_definitions = client.get_type_definitions()
        self.cache: dict[str, dict[str, Any]] = {}

    def get_entity(self, guid: str) -> dict[str, Any]:
        """Return a complete entity while minimizing calls with a local cache.

        Atlas entity responses commonly include complete tables and columns in
        ``referredEntities``. Caching those objects allows a large semantic
        model to be exported with only its initial dataset request.
        """

        if guid not in self.cache:
            response = self.client.get_entity(guid)
            entity = response.get("entity")
            if not entity:
                raise PurviewApiError(f"Purview returned no entity for GUID {guid}.")
            self.cache[guid] = entity
            for referred in (response.get("referredEntities") or {}).values():
                referred_guid = referred.get("guid")
                if referred_guid and not referred.get("isIncomplete", False):
                    self.cache.setdefault(referred_guid, referred)
        return self.cache[guid]

    def extract_node(
        self,
        entity: dict[str, Any],
        *,
        relationship_name: str,
        ancestors: frozenset[str],
    ) -> dict[str, Any]:
        """Recursively convert an entity and its schema relationships to a tree.

        ``ancestors`` prevents malformed or bidirectional Atlas relationships
        from creating recursion cycles. Child GUIDs are also deduplicated within
        each parent.
        """

        node = entity_summary(entity)
        node["kind"] = classify_schema_entity(entity, relationship_name)
        node["children"] = []

        guid = entity.get("guid")
        if not guid or guid in ancestors:
            return node

        full_entity = self.get_entity(guid)
        relationship_attributes = full_entity.get("relationshipAttributes") or {}
        allowed_names = schema_relationship_names(full_entity, self.type_definitions)
        next_ancestors = ancestors | {guid}
        seen_children: set[str] = set()

        for name, value in relationship_attributes.items():
            if name not in allowed_names and not any(
                marker in name.lower()
                for marker in ("schema", "table", "column", "field")
            ):
                continue
            for reference in iter_entity_references(value):
                child_guid = reference.get("guid")
                if (
                    not child_guid
                    or child_guid in seen_children
                    or child_guid in next_ancestors
                ):
                    continue
                seen_children.add(child_guid)
                child = self.get_entity(child_guid)
                node["children"].append(
                    self.extract_node(
                        child,
                        relationship_name=name,
                        ancestors=next_ancestors,
                    )
                )

        node["children"].sort(
            key=lambda child: (
                child.get("kind", ""),
                str(child.get("name") or "").lower(),
            )
        )
        return node

    def extract_dataset(self, guid: str) -> dict[str, Any]:
        """Extract one dataset and all reachable schema elements."""

        entity = self.get_entity(guid)
        result = self.extract_node(
            entity, relationship_name="dataset", ancestors=frozenset()
        )
        result["kind"] = "dataset"
        return result


def flatten_dataset(dataset: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield normalized CSV rows from a hierarchical dataset result.

    The current schema and table names are carried down the tree so every
    column row includes its parent context. Power BI models without a separate
    schema entity leave ``schemaName`` empty.
    """

    dataset_name = dataset.get("name")
    dataset_guid = dataset.get("guid")

    def visit(
        node: dict[str, Any],
        schema_name: str | None = None,
        table_name: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        """Walk one subtree while preserving its nearest schema and table."""

        kind = node.get("kind")
        if kind == "schema":
            schema_name = node.get("name")
        elif kind == "table":
            table_name = node.get("name")

        yield {
            "datasetName": dataset_name,
            "datasetGuid": dataset_guid,
            "schemaName": schema_name,
            "tableName": table_name,
            "elementKind": kind,
            "elementName": node.get("name"),
            "elementGuid": node.get("guid"),
            "entityType": node.get("typeName"),
            "qualifiedName": node.get("qualifiedName"),
            "dataType": (node.get("attributes") or {}).get("dataType")
            or (node.get("attributes") or {}).get("type"),
            "description": (node.get("attributes") or {}).get("description"),
        }
        for child in node.get("children", []):
            yield from visit(child, schema_name, table_name)

    for child in dataset.get("children", []):
        yield from visit(child)


def write_outputs(
    datasets: list[dict[str, Any]], json_path: Path, csv_path: Path
) -> None:
    """Write hierarchical JSON and normalized UTF-8 CSV output files."""

    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps({"datasets": datasets}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    fieldnames = [
        "datasetName",
        "datasetGuid",
        "schemaName",
        "tableName",
        "elementKind",
        "elementName",
        "elementGuid",
        "entityType",
        "qualifiedName",
        "dataType",
        "description",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for dataset in datasets:
            writer.writerows(flatten_dataset(dataset))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define and parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Export Power BI datasets and their schema/table/column metadata "
            "from Microsoft Purview Data Map."
        )
    )
    parser.add_argument("--env-file", type=Path, help="Path to a .env file.")
    parser.add_argument("--endpoint", help="Purview Atlas/Data Map endpoint.")
    parser.add_argument("--account-name", help="Microsoft Purview account name.")
    parser.add_argument(
        "--auth",
        choices=("auto", "service-principal", "azure"),
        default="auto",
        help=(
            "auto uses service-principal values when all are present, otherwise "
            "DefaultAzureCredential; azure always uses DefaultAzureCredential."
        ),
    )
    parser.add_argument(
        "--api-version",
        default=DEFAULT_API_VERSION,
        help=f"Data Map search API version (default: {DEFAULT_API_VERSION}).",
    )
    parser.add_argument(
        "--dataset-type",
        action="append",
        dest="dataset_types",
        help="Power BI dataset Atlas type. Repeat to query multiple types.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output") / "powerbi_datasets.json",
        help="Hierarchical JSON output path.",
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        default=Path("output") / "powerbi_dataset_schema.csv",
        help="Flattened CSV output path.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable informational logging."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the extraction workflow and return a process exit code."""

    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        settings = load_settings(args)
        credential = create_credential(settings)
        client = PurviewAtlasClient(
            settings.endpoint, credential, settings.api_version
        )
        dataset_types = client.find_dataset_types(args.dataset_types)
        LOGGER.info("Dataset entity types: %s", ", ".join(dataset_types))

        search_results: dict[str, dict[str, Any]] = {}
        for dataset_type in dataset_types:
            LOGGER.info("Searching for %s entities", dataset_type)
            for result in client.search_entities(dataset_type):
                guid = result.get("id") or result.get("guid")
                if guid:
                    # A dataset may match more than one compatible type query.
                    search_results[guid] = result

        extractor = SchemaExtractor(client)
        datasets = []
        for index, result in enumerate(search_results.values(), start=1):
            guid = result.get("id") or result.get("guid")
            LOGGER.info(
                "Extracting dataset %d/%d: %s",
                index,
                len(search_results),
                result.get("name") or guid,
            )
            datasets.append(extractor.extract_dataset(guid))

        datasets.sort(key=lambda item: str(item.get("name") or "").lower())
        write_outputs(datasets, args.output, args.csv_output)
        print(
            f"Exported {len(datasets)} Power BI dataset(s) to "
            f"{args.output} and {args.csv_output}."
        )
        return 0
    except (ValueError, PurviewApiError, requests.RequestException) as exc:
        LOGGER.error("%s", exc)
        return 1
    except Exception:
        LOGGER.exception("Unexpected failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
