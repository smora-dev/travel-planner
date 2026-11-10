"""Agente recolector: combina viajante + fli + trvl y guarda data/latest.json + data/history.csv.

Corre en GitHub Actions (necesita internet abierto a Google). Cada fuente puede fallar
sola; el resultado usa las que respondan y marca cuando no coinciden.
"""
import calendar, csv, json, math, os, subprocess, sys, tempfile
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
CUR, NIGHTS, ORIGIN = CFG["currency"], CFG["nights"], CFG["origin"]
SPREAD_FLAG = 0.10  # >10% de diferencia entre fuentes = ⚠️
os.environ.setdefault("TRVL_NO_TELEMETRY", "1")
os.environ.setdefault("TRVL_NO_BROWSER_COOKIES", "1")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run_json(cmd, save_flag=None, timeout=240):
    """Corre un CLI y devuelve su JSON (por stdout o por --save). None si falla."""
    try:
        if save_flag:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
                path = f.name
            subprocess.run(cmd + [save_flag, path], capture_output=True, text=True, timeout=timeout)
            with open(path) as f:
                return json.load(f)
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
        starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
        return json.loads(out[min(starts):]) if starts else None
    except Exception as e:
        log("  fallo", cmd[0], type(e).__name__, str(e)[:120])
        return None


def walk(obj):
    """Recorre cualquier JSON y produce todos los dicts (formatos de trvl/fli no son fijos)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


DATE_KEYS = ("departure_date", "date", "outbound_date", "departure", "start_date")
PRICE_KEYS = ("price", "min_price", "total_price", "amount", "fare")


def num(v):
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    if isinstance(v, dict):
        return num(v.get("amount") or v.get("value"))
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("$", "").split()[0])
        except (ValueError, IndexError):
            return None


def day_prices(data):
    """{YYYY-MM-DD: precio mínimo} sacado de cualquier JSON con fecha+precio."""
    out = {}
    for d in walk(data):
        ds = next((str(d[k])[:10] for k in DATE_KEYS if d.get(k)), None)
        p = next((num(d[k]) for k in PRICE_KEYS if num(d.get(k))), None)
        if ds and p and len(ds) == 10 and ds[4] == "-":
            out[ds] = min(p, out.get(ds, p))
    return out


# ---------- Vuelos ----------
def flights_month(airport, start, end):
    s, e = start.isoformat(), end.isoformat()
    src = {
        "viajante": day_prices(run_json(["viajante", "dates", f"{ORIGIN}-{airport}", "--from", s, "--to", e,
                                         "--nights", str(NIGHTS), "--currency", CUR, "--max-stops", "1"], "--save")),
        "fli": day_prices(run_json(["fli", "dates", ORIGIN, airport, "--from", s, "--to", e, "--round",
                                    "--duration", str(NIGHTS), "--currency", CUR, "--format", "json"])),
        "trvl": day_prices(run_json(["trvl", "dates", ORIGIN, airport, "--from", s, "--to", e, "--round-trip",
                                     "--duration", str(NIGHTS), "--currency", CUR, "--format", "json"])),
    }
    days = {}
    for name, prices in src.items():
        for ds, p in prices.items():
            if s <= ds <= e:
                days.setdefault(ds, {})[name] = p
    return {ds: v for ds, v in sorted(days.items())}, {k: len(v) for k, v in src.items()}


def best_day(days):
    """Día más barato; precio = mínimo entre fuentes; ⚠️ si las fuentes difieren >10%."""
    if not days:
        return None
    ds, srcs = min(days.items(), key=lambda kv: min(kv[1].values()))
    lo, hi = min(srcs.values()), max(srcs.values())
    return {"date": ds, "return": (date.fromisoformat(ds) + timedelta(days=NIGHTS)).isoformat(),
            "price": round(lo), "sources": {k: round(v) for k, v in srcs.items()},
            "check": len(srcs) > 1 and (hi - lo) / lo > SPREAD_FLAG,
            "url": f"https://www.google.com/travel/flights?q=Flights%20from%20{ORIGIN}%20to%20"
                   f"{{AIRPORT}}%20on%20{ds}%20through%20"
                   f"{(date.fromisoformat(ds) + timedelta(days=NIGHTS)).isoformat()}"}


# ---------- Hoteles ----------
def hotels(city, checkin, checkout):
    """Mejores calidad-precio: rating >= mínimo, ordenados por precio. Cuarto para 4 personas."""
    found = []
    v = run_json(["viajante", "hotels", city, checkin, checkout, "--currency", CUR, "--adults", "4",
                  "--source", "google", "--min-rating", str(CFG["min_hotel_rating"]), "--top", "10"], "--save")
    for h in walk(v):
        if "title" in h and num(h.get("total_price")):
            found.append({"name": h["title"], "total": num(h["total_price"]), "rating": h.get("rating_score"),
                          "reviews": h.get("review_count"), "url": h.get("link"), "src": "viajante"})
    t = run_json(["trvl", "hotels", city, "--checkin", checkin, "--checkout", checkout, "--guests", "4",
                  "--currency", CUR, "--sort", "cheapest", "--format", "json"])
    for h in walk(t):
        name, price, rating = h.get("name"), num(h.get("price")), num(h.get("rating"))
        if name and price and rating and "hotel_id" in h:  # resultado de hotel (no tipo de cuarto)
            r5 = rating / 2 if rating > 5 else rating
            found.append({"name": name, "total": price * NIGHTS, "rating": round(r5, 1),  # trvl: precio por noche
                          "reviews": h.get("review_count") or h.get("reviews"),
                          "url": h.get("booking_url") or h.get("url"), "src": "trvl"})
    good = [h for h in found if (h["rating"] or 0) >= CFG["min_hotel_rating"] and (h["reviews"] or 100) >= 100]
    good.sort(key=lambda h: h["total"])
    seen, top = set(), []
    for h in good:  # mismo hotel en dos fuentes: queda el más barato
        k = h["name"].lower()[:30]
        if k not in seen:
            seen.add(k)
            h["total"] = round(h["total"])
            top.append(h)
    return top[:3]


# ---------- Carros ----------
def cars(airport, pickup, dropoff):
    out = {}
    for label, pax in (("small", 5), ("van", 9)):
        data = run_json(["trvl", "cars", airport, pickup, dropoff, "--currency", CUR, "--passengers", str(pax),
                         "--format", "json"])
        offers = [o for o in walk(data) if num(o.get("price")) and (o.get("passengers") or o.get("seats") or 5) >= pax]
        if offers:
            o = min(offers, key=lambda o: num(o["price"]))
            out[label] = {"price": round(num(o["price"]) + (num(o.get("taxes_and_fees")) or 0)),
                          "name": o.get("vehicle_name") or o.get("vehicle_class"), "url": o.get("booking_url"),
                          "provider": o.get("supplier") or o.get("provider")}
    return out


def months_ahead(n):
    t = date.today()
    y, m = t.year, t.month
    for _ in range(n):
        last = calendar.monthrange(y, m)[1]
        start = max(date(y, m, 1), t + timedelta(days=3))
        yield start, date(y, m, last)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    only = sys.argv[1:]  # opcional: python collector.py orlando
    out = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"), "demo": False,
           "currency": CUR, "nights": NIGHTS, "origin": ORIGIN, "destinations": []}
    hist_rows = []
    for dest in CFG["destinations"]:
        if only and dest["id"] not in only:
            continue
        log("==", dest["name"])
        months = []
        for start, end in months_ahead(CFG["months_ahead"]):
            days, counts = flights_month(dest["airport"], start, end)
            fl = best_day(days)
            log(" ", start.strftime("%Y-%m"), counts, fl and fl["price"])
            m = {"month": start.strftime("%Y-%m"), "flight": fl,
                 "days": {ds: round(min(v.values())) for ds, v in days.items()}}
            if fl:
                fl["url"] = fl["url"].replace("{AIRPORT}", dest["airport"])
                m["hotels"] = hotels(dest["hotel_city"], fl["date"], fl["return"])
                if dest.get("car"):
                    m["car"] = cars(dest["airport"], fl["date"], fl["return"])
                hist_rows.append([out["updated_at"][:10], dest["id"], m["month"], fl["date"], fl["price"],
                                  m["hotels"][0]["total"] if m["hotels"] else ""])
            months.append(m)
        out["destinations"].append({**dest, "months": months})

    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    if not any(m["flight"] for d in out["destinations"] for m in d["months"]):
        log("Ninguna fuente devolvió precios; no se sobreescribe latest.json")
        sys.exit(1)
    json.dump(out, open(os.path.join(ROOT, "data", "latest.json"), "w"), ensure_ascii=False, indent=1)
    hist = os.path.join(ROOT, "data", "history.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_date", "dest", "month", "best_date", "flight_pp", "hotel_total"])
        w.writerows(hist_rows)


if __name__ == "__main__":
    main()
