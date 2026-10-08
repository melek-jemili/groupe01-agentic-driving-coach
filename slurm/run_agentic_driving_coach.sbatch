#!/bin/bash
# Agentic Driving Coach - Sol batch job (Part 3 model benchmark).

#SBATCH --job-name=agentic-driving-coach
# The account depends on which allocation you belong to: students use the
# class account below; research-group members may use their group account
# instead.
#SBATCH --account=class_cse494598fall2026
#SBATCH --partition=htc
#SBATCH --qos=class
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=24G                      # Sol requires >= 24000 MB for GPU jobs
#SBATCH --time=00:45:00
#SBATCH --output=slurm-%j.out

set -euo pipefail

# --- site setup ------------------------------------------------------------
module load apptainer/1.4.5

PROJECT_DIR="${PROJECT_DIR:-$SLURM_SUBMIT_DIR}"
SIF="${SIF:-/scratch/$USER/agentic-driving-coach.sif}"
export OLLAMA_MODELS=/scratch/$USER/ollama-models
export OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_BIN=/scratch/$USER/ollama/bin/ollama
# Make the ollama CLI visible to environment capture (host side and, where
# /scratch is bind-mounted, inside the container too).
export PATH="$(dirname "$OLLAMA_BIN"):$PATH"

MODELS="${MODELS:-llama3.2:1b llama3.2:3b}"
# The assignment requires a 1,700 ms deadline on Sol. Override without editing:
#   sbatch --export=ALL,DEADLINE_MS=<value> slurm/run_agentic_driving_coach.sbatch
DEADLINE_MS="${DEADLINE_MS:-1700}"
REPETITIONS="${REPETITIONS:-3}"
WARMUP_TIMEOUT_S="${WARMUP_TIMEOUT_S:-600}"   # cold loads from scratch take minutes

cd "$PROJECT_DIR"

# --- 1. start the Ollama server inside this allocation ---------------------
"$OLLAMA_BIN" serve > "ollama-$SLURM_JOB_ID.log" 2>&1 &
OLLAMA_PID=$!
trap 'kill "$OLLAMA_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
    if curl -sf "$OLLAMA_HOST/api/version" > /dev/null; then break; fi
    sleep 1
done
curl -sf "$OLLAMA_HOST/api/version" || { echo "Ollama did not start"; exit 3; }

# --- 2. verify models are already pulled (this job never downloads) --------
for m in $MODELS; do
    if ! "$OLLAMA_BIN" list | grep -q "^$m"; then
        echo "model $m missing; run once beforehand: ollama pull $m" >&2
        exit 3
    fi
done

# --- 3. environment capture + doctor --------------------------------------
apptainer exec --nv "$SIF" env PYTHONPATH=src \
    python scripts/check_environment.py --live

# --- 4. pre-load both models and log their load times ----------------------
# /api/version proves the server is up, NOT that a model is resident. Cold
# loads exceed the in-run 30 s request timeout, and Ollama aborts a load when
# its client disconnects, so an explicit bounded warm-up is required.
# benchmark-models also re-warms automatically before
# every run; this step fails fast and makes load times visible in the log.
apptainer exec --nv "$SIF" env PYTHONPATH=src OLLAMA_HOST="$OLLAMA_HOST" \
    python -m agentic_driving_coach doctor --live --warm --models $MODELS \
        --warmup-timeout-s "$WARMUP_TIMEOUT_S"

# --- 5. the benchmark ------------------------------------------------------
apptainer exec --nv "$SIF" env PYTHONPATH=src OLLAMA_HOST="$OLLAMA_HOST" MPLBACKEND=Agg \
    python -m agentic_driving_coach benchmark-models \
        --models $MODELS \
        --driver beginner \
        --repetitions "$REPETITIONS" \
        --deadline-ms "$DEADLINE_MS" \
        --warmup-timeout-s "$WARMUP_TIMEOUT_S" \
        --output "results/model-comparison-$SLURM_JOB_ID"

# --- 6. require numeric latency samples (guards the cold-load failure mode)
apptainer exec "$SIF" env PYTHONPATH=src \
    RESULTS_DIR="results/model-comparison-$SLURM_JOB_ID" python - <<'PY'
import json, os, sys
rows = json.load(open(os.environ["RESULTS_DIR"] + "/comparison.json"))
missing = sorted({r["model"] for r in rows if r.get("latency_median_ms") is None})
if missing:
    sys.exit("ERROR: no numeric latency samples for: " + ", ".join(missing)
             + " - warm-up failed? See the ollama-*.log in this directory.")
print("numeric latency samples present for every run")
PY

echo "done: results/model-comparison-$SLURM_JOB_ID"
