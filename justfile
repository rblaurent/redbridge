set windows-shell := ["bash", "-cu"]

# Step-2 smoke test: opens deck, clears it, prints inputs.
deck-test:
    cd daemon && uv run python deck.py

dev-daemon:
    cd daemon && uv run uvicorn main:app --host 127.0.0.1 --port 47337 --reload

dev-webui:
    cd webui && npm run dev

build:
    cd webui && npm install && npm run build

run:
    cd daemon && uv run python main.py
