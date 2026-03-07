Installation instructions

Goal: Provide safe Windows-friendly steps to install either the lightweight runtime (default) or the full feature set (LangGraph, LangChain, pandas).

Recommended approach (Windows):

1. Option A — Quick & reliable (recommended): Use the lightweight runtime

- Create and activate a virtual environment (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

- Install lightweight requirements (fast, avoids numpy/pandas build):

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

This will allow running the app in a simulated/default mode (dummy LLM/workflow) without heavy native builds.

2. Option B — Full features (LangGraph, LangChain, pandas, numpy)

Note: The full set requires binary wheels for `numpy` and friends. On Windows the easiest path is to use Conda (miniconda/Anaconda) or use a Python version that has prebuilt wheels (e.g., 3.11/3.12 at the time of writing).

- Using Conda (recommended for Windows):

```powershell
# Create conda env
conda create -n outreach python=3.11 -y
conda activate outreach

# Install binary packages with conda first (numpy, pandas)
conda install numpy pandas -y

# Then pip-install the rest
pip install --upgrade pip
pip install -r requirements-full.txt
```

- Using pip only (if you must):
  - Use Python 3.11 or 3.12, not 3.14, to maximize wheel availability.
  - If pip tries to build numpy from source, install a compatible wheel first or use a prebuilt wheel provider.

Troubleshooting:

- If `pip install -r requirements-full.txt` fails on `numpy` with Meson errors, prefer the Conda route above.
- If you need help choosing a Python version, use `python --version` to check.

Runtime notes:

- The codebase supports a "lite" mode where missing optional deps (LangGraph, LangChain, Groq client) will be replaced by safe fallbacks so you can run and test without cloud LLM credentials.
- To enable real LLM + workflow features, set environment variables in a `.env` file (copy `.env.example` to `.env`) and install the full requirements.
