"""Write a static copy of the dashboard to docs/ for GitHub Pages (used by the cloud workflow)."""
import json
import os
import shutil
import time

from dashboard import HERE, live_payload, backtest_payload

DOCS = os.path.join(HERE, "docs")


def main():
    os.makedirs(os.path.join(DOCS, "data"), exist_ok=True)
    shutil.copyfile(os.path.join(HERE, "dashboard.html"), os.path.join(DOCS, "index.html"))
    for view in ("paper", "paper_ema90"):
        json.dump(live_payload(view), open(os.path.join(DOCS, "data", f"{view}.json"), "w"))
    bt = os.path.join(DOCS, "data", "backtest.json")
    if not os.path.exists(bt) or time.time() - os.path.getmtime(bt) > 86400:
        json.dump(backtest_payload(), open(bt, "w"))
    print("exported dashboard to docs/")


if __name__ == "__main__":
    main()
