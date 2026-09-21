# Microsoft Purview Power BI Dataset Extractor

This Python CLI queries Microsoft Purview Data Map for Power BI datasets and
retrieves their associated schema, table, and column entities through the Atlas
API. It produces:

- Hierarchical JSON preserving the dataset/schema/table/column relationships.
- A flattened CSV suitable for reporting or further analysis.

The extractor reads Atlas type definitions at runtime, discovers Power BI
dataset type names, and follows each type's `schemaElementsAttribute`. It also
supports common relationship names such as `schemas`, `tables`, `columns`, and
`fields`, which makes it tolerant of differences between Purview type versions.

## Prerequisites

- Python 3.10 or later.
- A Microsoft Purview account with Power BI/Fabric metadata already scanned
  into the Data Map.
- Data Map permissions that allow the identity to search and read assets.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Authentication

Copy `.env.example` to `.env` and set `PURVIEW_ENDPOINT` or
`PURVIEW_ACCOUNT_NAME`.

### Service principal from `.env`

Set these values:

```dotenv
PURVIEW_ENDPOINT=https://your-account.purview.azure.com
PURVIEW_TENANT_ID=your-tenant-id
PURVIEW_CLIENT_ID=your-client-id
PURVIEW_CLIENT_SECRET=your-client-secret
```

Run:

```powershell
python .\purview_powerbi_extract.py --auth service-principal --verbose
```

`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `AZURE_CLIENT_SECRET` are accepted as
alternatives. Never commit the populated `.env` file.

### Azure authentication

Sign in with Azure CLI, Visual Studio Code, Azure PowerShell, workload identity,
or another credential supported by `DefaultAzureCredential`, then run:

```powershell
az login
python .\purview_powerbi_extract.py --auth azure --verbose
```

For local interactive-browser fallback, set
`PURVIEW_ALLOW_INTERACTIVE_BROWSER=true`.

The default `--auth auto` mode uses service-principal credentials when all three
values are present; otherwise it uses `DefaultAzureCredential`.

## Usage

```powershell
python .\purview_powerbi_extract.py `
  --env-file .\.env `
  --output .\output\powerbi_datasets.json `
  --csv-output .\output\powerbi_dataset_schema.csv `
  --verbose
```

The program normally discovers the dataset Atlas type from Purview's type
definitions. If your account uses a custom or unexpected type, specify it:

```powershell
python .\purview_powerbi_extract.py `
  --dataset-type azure_powerbi_dataset `
  --dataset-type custom_powerbi_semantic_model
```

Run `python .\purview_powerbi_extract.py --help` for all options.

## Output

The JSON contains complete useful attributes, classifications, labels,
contacts, and nested schema elements. The CSV contains one row per schema,
table, column, or other schema element and includes its parent dataset,
schema/table context, GUID, Atlas type, qualified name, data type, and
description.

Power BI semantic models do not always expose a separate relational-schema
entity. In that case, tables and columns are nested directly beneath the
dataset, and `schemaName` is empty in the CSV.
