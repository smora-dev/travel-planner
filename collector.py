"""Agente recolector: combina viajante + fli + trvl y guarda data/latest.json + data/history.csv.

Corre en GitHub Actions (necesita internet abierto a Google). Cada fuente puede fallar
sola; el resultado usa las que respondan y marca cuando no coinciden.
"""
import calendar, csv, glob, json, os, subprocess, sys, tempfile, time
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
CUR, NIGHTS, ORIGIN = CFG["currency"], CFG["nights"], CFG["origin"]
SPREAD_FLAG = 0.10  # >10% de diferencia entre fuentes = ⚠️
os.environ.setdefault("TRVL_NO_TELEMETRY", "1")
os.environ.setdefault("TRVL_NO_BROWSER_COOKIES", "1")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


PAUSE = float(os.environ.get("PAUSE", "6"))  # segundos entre consultas: evita que Google bloquee


def run_json(cmd, save_flag=None, timeout=240):
    """Corre un CLI y devuelve su JSON (por stdout o por --save). None si falla."""
    time.sleep(PAUSE)
    p = None
    try:
        if save_flag:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
                path = f.name
            p = subprocess.run(cmd + [save_flag, path], capture_output=True, text=True, timeout=timeout)
            with open(path) as f:
                return json.load(f)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = p.stdout
        starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
        run_json.last = out[:400]
        if not starts:
            log("  vacío", cmd[:2], (p.stderr or out)[-300:].replace("\n", " "))
        return json.loads(out[min(starts):]) if starts else None
    except Exception as e:
        err = (p.stderr if p else "")[-300:].replace("\n", " ")
        log("  fallo", cmd[:2], type(e).__name__, str(e)[:120], err)
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
        "trvl": day_prices(run_json(["trvl", "dates", ORIGIN, airport, "--from", s, "--to", e, "--round-trip",
                                     "--duration", str(NIGHTS), "--currency", CUR, "--format", "json"])),
        "fli": day_prices(run_json(["fli", "dates", ORIGIN, airport, "--from", s, "--to", e, "--round",
                                    "--duration", str(NIGHTS), "--currency", CUR, "--format", "json"])),
    }
    if not src["trvl"] and not src["fli"]:  # bloqueo temporal: espera y reintenta una vez
        log("  sin datos (Google en enfriamiento), reintento en 5 min")
        time.sleep(300)
        src["trvl"] = day_prices(run_json(["trvl", "dates", ORIGIN, airport, "--from", s, "--to", e, "--round-trip",
                                           "--duration", str(NIGHTS), "--currency", CUR, "--format", "json"]))
    if not src["fli"]:
        log("  fli sin precios; muestra:", json.dumps(run_json.last)[:300] if hasattr(run_json, "last") else "")
    cal = {}
    for prices in src.values():
        for ds, p in prices.items():
            cal[ds] = min(p, cal.get(ds, p))
    # viajante: verifica el mejor día del calendario (o barre el mes si ninguna fuente respondió)
    vfrom, vto = (min(cal, key=cal.get),) * 2 if cal else (s, e)
    src["viajante"] = day_prices(run_json(["viajante", "dates", f"{ORIGIN}-{airport}", "--from", vfrom, "--to", vto,
                                           "--nights", str(NIGHTS), "--currency", CUR, "--max-stops", "1"], "--save"))
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


def alt_nights(airport, start, end):
    """Otras duraciones (4, 10 noches...): mejor día del mes según el calendario de trvl.
    ponytail: solo trvl para no triplicar consultas; hotel y carro se escalan por noche en el dashboard."""
    out = {}
    for n in CFG.get("extra_nights", []):
        days = day_prices(run_json(["trvl", "dates", ORIGIN, airport, "--from", start.isoformat(), "--to",
                                    end.isoformat(), "--round-trip", "--duration", str(n), "--currency", CUR,
                                    "--format", "json"]))
        days = {d: p for d, p in days.items() if start.isoformat() <= d <= end.isoformat()}
        if days:
            d = min(days, key=days.get)
            ret = (date.fromisoformat(d) + timedelta(days=n)).isoformat()
            out[str(n)] = {"date": d, "return": ret, "price": round(days[d]),
                           "url": f"https://www.google.com/travel/flights?q=Flights%20from%20{ORIGIN}%20to%20{airport}%20on%20{d}%20through%20{ret}"}
    return out


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


# ---------- Carros (OctoTrip MCP: gratis, sin key, varias rentadoras) ----------
OCTO = "https://mcp.octotrip.app/rental-cars/mcp"
UA = "Mozilla/5.0 (compatible; travel-planner/1.0; +https://github.com/smora-dev/travel-planner)"


def mcp_call(url, tool, args):
    """Llamada mínima a un servidor MCP por HTTP (initialize + tools/call). Acepta JSON o SSE."""
    import urllib.request
    sid = None

    def post(body):
        nonlocal sid
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": UA}
        if sid:
            h["Mcp-Session-Id"] = sid
        r = urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), h), timeout=120)
        sid = r.headers.get("Mcp-Session-Id") or sid
        raw = r.read().decode()
        if not raw.strip():
            return None
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        datas = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")]
        return json.loads(datas[-1]) if datas else None

    post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "travel-planner", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    res = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    res = (res or {}).get("result") or {}
    sc = res.get("structuredContent")
    if isinstance(sc, dict) and isinstance(sc.get("result"), str):
        try:
            return json.loads(sc["result"])
        except ValueError:
            pass
    if sc:
        return sc
    for c in res.get("content", []):
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except ValueError:
                log("  octotrip texto:", c["text"][:200])
    return None


def cars(airport, pickup, dropoff, location=None):
    """Carro más barato que quepa: 'small' hasta 5 personas, 'van' para 6-9. Total por todo el alquiler."""
    time.sleep(PAUSE)
    try:
        data = mcp_call(OCTO, "search", {"location": location or f"{airport} Airport", "pickup_date": pickup,
                                         "dropoff_date": dropoff, "currency": CUR, "pickup_time": "12:00",
                                         "dropoff_time": "12:00"})
    except Exception as e:
        log("  fallo octotrip", type(e).__name__, str(e)[:200])
        return {}
    offers = [o for o in (data or {}).get("results", []) if num(o.get("price"))]
    if not offers:
        log("  octotrip sin carros:", json.dumps(data)[:300])
    out = {}
    for label, lo, hi in (("small", 4, 5), ("van", 7, 99)):
        fit = [o for o in offers if lo <= (o.get("passengers") or 0) <= hi]
        if fit:
            o = min(fit, key=lambda o: num(o["price"]))
            out[label] = {"price": round(num(o["price"])), "name": f'{o.get("name", "")} ({o.get("category", "")})',
                          "provider": o.get("vendor"), "url": o.get("booking_url"),
                          "seats": o.get("passengers"), "free_cancel": o.get("free_cancellation")}
    return out


# ---------- Actividades (piqod + tickadoo + TripWays: gratis, sin key) ----------
FX = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "MXN": 0.055, "COP": 0.00025, "ARS": 0.001, "PEN": 0.27}  # ponytail: tasas fijas aprox., cambiar por API si hace falta exactitud
NAME_K = ("title", "name", "product_name", "experience_name")
PRICE_K = ("price", "from_price", "price_from", "min_price", "lowest_price", "price_usd", "amount")
URL_K = ("booking_url", "redirect_url", "checkout_url", "url", "link", "deeplink", "product_url", "booking_link")
RATE_K = ("rating", "review_score", "average_rating", "rating_avg", "score")
REV_K = ("review_count", "reviews_count", "num_reviews", "rating_count", "reviews")


def first(d, keys):
    return next((d[k] for k in keys if d.get(k) not in (None, "", [])), None)


def activities(dest):
    city = dest["name"].split(" / ")[0]
    slug = city.lower().replace(" ", "-").replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    calls = [("piqod", "https://piqod.it/mcp", "get_destination_guide", {"place": city, "limit": 15}),
             ("tickadoo", "https://mcp.tickadoo.com/mcp", "get_city_highlights", {"city": slug, "format": "json", "language": "es"}),
             ("tickadoo", "https://mcp.tickadoo.com/mcp", "get_family_day", {"city": slug, "format": "json", "language": "es"}),
             ("tripways", "https://mcp.tripways.com/mcp", "search_tours", {"query": f"best things to do in {city}", "destination": city})]
    found = []
    for src, url, tool, args in calls:
        time.sleep(PAUSE)
        try:
            data = mcp_call(url, tool, args)
        except Exception as e:
            log("  fallo", src, type(e).__name__, str(e)[:150])
            continue
        n0 = len(found)
        for d in walk(data):
            name, p = first(d, NAME_K), first(d, PRICE_K)
            cur = "USD"
            if isinstance(p, dict):
                cur = p.get("currency") or cur
                p = p.get("amount") or p.get("value") or ((p.get("amount_minor") or 0) / 100)
            cur = (d.get("currency") or cur).upper() if isinstance(d.get("currency") or cur, str) else "USD"
            p = num(p)
            if not (isinstance(name, str) and p):
                continue
            r, revs = first(d, RATE_K), first(d, REV_K)
            rating = num(r)
            if isinstance(r, dict):  # tripways: {"value": 4.8, "count": 120}
                rating, revs = num(r.get("average") or r.get("value")), r.get("count", revs)
            if rating and rating > 5:
                rating /= 2
            found.append({"name": name.strip(), "price": round(p * FX.get(cur, 1.0)), "orig": f"{round(p)} {cur}",
                          "rating": round(rating, 1) if rating else None, "reviews": revs if isinstance(revs, (int, float)) else num(revs),
                          "url": first(d, URL_K), "src": src})
        log(f"  {src}.{tool}: {len(found) - n0} actividades")
        if len(found) == n0:
            log("    muestra:", json.dumps(data)[:300])
    best = {}
    for a in found:  # misma actividad en varias fuentes: queda la más barata
        k = "".join(ch for ch in a["name"].lower() if ch.isalnum())[:40]
        if k not in best or a["price"] < best[k]["price"]:
            best[k] = a
    good = [a for a in best.values()
            if (a["rating"] is None or a["rating"] >= 4.3) and (a["reviews"] is None or a["reviews"] >= 50)]
    good.sort(key=lambda a: (-(a["rating"] or 4.5) * min((a["reviews"] or 50), 2000) ** 0.25, a["price"]))
    top = good[:12]
    for a in top:
        if a["src"] == "piqod":
            a["name"] = translate_es(a["name"])
    return top


def translate_es(text):
    """Traduce al español (Google público, si no MyMemory); si ambos fallan, deja el original."""
    import urllib.parse, urllib.request
    q = urllib.parse.quote(text)
    get = lambda u: json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=20))
    try:
        r = get("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=es&dt=t&q=" + q)
        return "".join(seg[0] for seg in r[0] if seg and seg[0]) or text
    except Exception:
        pass
    try:
        r = get(f"https://api.mymemory.translated.net/get?q={q}&langpair=it|es")
        t = r.get("responseData", {}).get("translatedText")
        return t if t and r.get("responseStatus") == 200 else text
    except Exception as e:
        log("  traducción falló:", type(e).__name__)
        return text


def months_ahead(n):
    t = date.today()
    y, m = t.year, t.month
    for _ in range(n):
        last = calendar.monthrange(y, m)[1]
        start = max(date(y, m, 1), t + timedelta(days=3))
        yield start, date(y, m, last)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


DATA = os.path.join(ROOT, "data")


def collect(only):
    """python collector.py orlando -> data/parts/orlando.json"""
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
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
                m["alt"] = alt_nights(dest["airport"], start, end)
                fl["url"] = fl["url"].replace("{AIRPORT}", dest["airport"])
                fl["seen"] = now
                m["hotels"] = hotels(dest["hotel_city"], fl["date"], fl["return"])
                if dest.get("car"):
                    m["car"] = cars(dest["airport"], fl["date"], fl["return"], dest.get("car_location"))
            months.append(m)
        acts = activities(dest)
        log("  actividades:", len(acts))
        os.makedirs(os.path.join(DATA, "parts"), exist_ok=True)
        json.dump({**dest, "months": months, "activities": acts}, open(os.path.join(DATA, "parts", dest["id"] + ".json"), "w"),
                  ensure_ascii=False)


def merge():
    """Une data/parts/*.json en latest.json. Si un mes falló hoy, conserva el último precio bueno."""
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    old_acts = {}
    try:
        old_acts = {d["id"]: d.get("activities", []) for d in json.load(open(os.path.join(DATA, "latest.json")))["destinations"]}
        old = {d["id"]: {m["month"]: m for m in d["months"]} for d in json.load(open(os.path.join(DATA, "latest.json")))["destinations"]}
        if json.load(open(os.path.join(DATA, "latest.json"))).get("demo"):
            old = {}
    except (OSError, ValueError, KeyError):
        old = {}
    parts = {}
    for p in glob.glob(os.path.join(DATA, "parts", "*.json")):
        d = json.load(open(p))
        parts[d["id"]] = d
    out = {"updated_at": now, "demo": False, "currency": CUR, "nights": NIGHTS, "extra_nights": CFG.get("extra_nights", []),
           "origin": ORIGIN, "destinations": []}
    hist_rows, fresh = [], 0
    for dest in CFG["destinations"]:
        new = {m["month"]: m for m in parts.get(dest["id"], {}).get("months", [])}
        months = []
        for start, _ in months_ahead(CFG["months_ahead"]):
            k = start.strftime("%Y-%m")
            m = new.get(k)
            if m and m.get("flight"):
                fresh += 1
                f = m["flight"]
                hist_rows.append([now[:10], dest["id"], k, f["date"], f["price"],
                                  m["hotels"][0]["total"] if m.get("hotels") else ""])
            elif old.get(dest["id"], {}).get(k, {}).get("flight") and old[dest["id"]][k]["flight"]["date"] >= now[:10]:
                m = old[dest["id"]][k]  # precio anterior; el dashboard muestra de cuándo es (flight.seen)
            else:
                m = m or {"month": k, "flight": None, "days": {}}
            months.append(m)
        acts = parts.get(dest["id"], {}).get("activities") or old_acts.get(dest["id"], [])
        out["destinations"].append({**dest, "months": months, "activities": acts})
    log(f"merge: {fresh} meses con precio nuevo")
    if not fresh:
        log("Ninguna fuente devolvió precios; no se toca latest.json")
        sys.exit(1)
    json.dump(out, open(os.path.join(DATA, "latest.json"), "w"), ensure_ascii=False, indent=1)
    hist = os.path.join(DATA, "history.csv")
    first = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if first:
            w.writerow(["run_date", "dest", "month", "best_date", "flight_pp", "hotel_total"])
        w.writerows(hist_rows)


def main():
    if sys.argv[1:] == ["--merge"]:
        merge()
    else:
        collect(sys.argv[1:])


if __name__ == "__main__":
    main()
