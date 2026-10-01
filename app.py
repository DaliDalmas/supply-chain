"""Supply chain analytics dashboard.

Run:  python app.py   ->  http://127.0.0.1:5000
Data: put your CSV at data/supply_chain_data.csv, or upload one in the browser.
"""
import io
import os

import pandas as pd
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB upload cap

DEFAULT_CSV = os.environ.get(
    "DATA_CSV", os.path.join(os.path.dirname(__file__), "data", "supply_chain_data.csv")
)
REQUIRED = ["product_type", "sku", "price", "number_of_products_sold", "revenue_generated"]
STATE = {"df": None, "name": None}  # single-user, in-memory


# ---------- loading ----------
def clean(df: pd.DataFrame) -> pd.DataFrame:
    """'Product type' -> 'product_type', so both 'Lead times' and 'Lead time' stay distinct."""
    df = df.copy()
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError("CSV is missing required columns: " + ", ".join(missing))
    for c in df.columns:
        if c not in ("product_type", "sku") and df[c].dtype == object:
            df[c] = df[c].astype(str).str.strip()
    return df


def load_default():
    if os.path.exists(DEFAULT_CSV):
        STATE["df"] = clean(pd.read_csv(DEFAULT_CSV))
        STATE["name"] = os.path.basename(DEFAULT_CSV)


# ---------- analytics ----------
def group(df, by, **aggs):
    """Group df by `by`; aggs are name=(column, func). Skips aggregations whose column is absent."""
    if by not in df.columns:
        return []
    spec = {k: v for k, v in aggs.items() if v[0] in df.columns}
    out = df.groupby(by).agg(**spec).reset_index().rename(columns={by: "name"})
    return out.round(2).to_dict("records")


def num(df, c, fn="sum"):
    return round(float(getattr(df[c], fn)()), 2) if c in df.columns else None


def findings(df):
    notes = []
    rev = group(df, "product_type", revenue=("revenue_generated", "sum"))
    if rev:
        top = max(rev, key=lambda r: r["revenue"])
        share = top["revenue"] / df["revenue_generated"].sum() * 100
        notes.append(f"{top['name'].title()} brings in the most revenue: {share:.0f}% of the total.")
    sup = group(df, "supplier_name", defect=("defect_rates", "mean"))
    if len(sup) > 1:
        worst, best = max(sup, key=lambda r: r["defect"]), min(sup, key=lambda r: r["defect"])
        notes.append(f"{worst['name']} has the highest average defect rate ({worst['defect']}%); "
                     f"{best['name']} has the lowest ({best['defect']}%).")
    car = group(df, "shipping_carriers", cost=("shipping_costs", "mean"))
    if len(car) > 1:
        c = max(car, key=lambda r: r["cost"])
        notes.append(f"{c['name']} is the most expensive carrier at {c['cost']} per shipment on average.")
    if "inspection_results" in df.columns:
        fail = (df["inspection_results"].str.lower() == "fail").mean() * 100
        notes.append(f"{fail:.0f}% of SKUs failed inspection.")
    if {"stock_levels", "order_quantities"} <= set(df.columns):
        n = int((df["stock_levels"] < df["order_quantities"]).sum())
        notes.append(f"{n} SKUs have less stock on hand than their usual order quantity.")
    return notes


def analytics(df):
    kpis = {
        "revenue": num(df, "revenue_generated"),
        "units": int(df["number_of_products_sold"].sum()),
        "skus": int(df["sku"].nunique()),
        "avg_price": num(df, "price", "mean"),
        "defect": num(df, "defect_rates", "mean"),
        "ship_cost": num(df, "shipping_costs", "mean"),
        "lead_time": num(df, "lead_times", "mean"),
    }
    supplier = group(
        df, "supplier_name",
        skus=("sku", "count"),
        revenue=("revenue_generated", "sum"),
        defect=("defect_rates", "mean"),
        lead_time=("lead_times", "mean"),
        mfg_cost=("manufacturing_costs", "mean"),
    )
    if "inspection_results" in df.columns and "supplier_name" in df.columns:
        fails = df[df["inspection_results"].str.lower() == "fail"].groupby("supplier_name").size()
        for r in supplier:
            r["fails"] = int(fails.get(r["name"], 0))

    risk, top = [], []
    if {"stock_levels", "order_quantities"} <= set(df.columns):
        r = df[df["stock_levels"] < df["order_quantities"]].copy()
        r["gap"] = r["order_quantities"] - r["stock_levels"]
        cols = [c for c in ["sku", "product_type", "stock_levels", "order_quantities", "gap", "supplier_name"] if c in r]
        risk = r.sort_values("gap", ascending=False).head(10)[cols].to_dict("records")
    t = df.sort_values("revenue_generated", ascending=False).head(10)
    cols = [c for c in ["sku", "product_type", "price", "number_of_products_sold", "revenue_generated"] if c in t]
    top = t[cols].round(2).to_dict("records")

    scatter = df[["price", "number_of_products_sold", "product_type"]].round(2).rename(
        columns={"number_of_products_sold": "units"}).to_dict("records")

    return {
        "kpis": kpis,
        "findings": findings(df),
        "by_product": group(df, "product_type", revenue=("revenue_generated", "sum"),
                            units=("number_of_products_sold", "sum")),
        "by_demographic": group(df, "customer_demographics", revenue=("revenue_generated", "sum")),
        "by_location": group(df, "location", revenue=("revenue_generated", "sum")),
        "by_carrier": group(df, "shipping_carriers", cost=("shipping_costs", "mean"),
                            days=("shipping_times", "mean")),
        "by_mode": group(df, "transportation_modes", cost=("costs", "mean"), skus=("sku", "count")),
        "by_inspection": group(df, "inspection_results", count=("sku", "count")),
        "supplier": supplier,
        "risk": risk,
        "top": top,
        "scatter": scatter,
    }


# ---------- routes ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analytics")
def api_analytics():
    df = STATE["df"]
    if df is None:
        return jsonify({"empty": True})
    pt = request.args.get("product_type")
    view = df[df["product_type"] == pt] if pt else df
    if view.empty:
        return jsonify({"error": f"No rows for product type '{pt}'."}), 404
    data = analytics(view)
    data["file"] = STATE["name"]
    data["rows"] = len(view)
    data["product_types"] = sorted(df["product_type"].dropna().unique().tolist())
    return jsonify(data)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "Choose a .csv file."}), 400
    try:
        df = clean(pd.read_csv(io.BytesIO(f.read())))
    except Exception as e:  # bad encoding, empty file, missing columns...
        return jsonify({"error": str(e)}), 400
    STATE["df"], STATE["name"] = df, f.filename
    return jsonify({"ok": True, "rows": len(df)})


load_default()

if __name__ == "__main__":
    app.run(debug=True)
