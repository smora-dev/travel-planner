import collector as c

# viajante-like, fli-like y trvl-like: formatos distintos, mismo parser
v = {"days": [{"departure_date": "2027-02-03", "price": 410.0}, {"departure_date": "2027-02-04", "price": None}]}
f = [{"date": "2027-02-03T00:00:00", "price": "$455"}, {"date": "2027-02-10", "price": 390}]
assert c.day_prices(v) == {"2027-02-03": 410.0}
assert c.day_prices(f) == {"2027-02-03": 455.0, "2027-02-10": 390.0}

b = c.best_day({"2027-02-03": {"viajante": 410, "fli": 455}, "2027-02-10": {"fli": 390}})
assert b["date"] == "2027-02-10" and b["price"] == 390 and b["return"] == "2027-02-17" and not b["check"]
b = c.best_day({"2027-02-03": {"viajante": 410, "fli": 470}})
assert b["price"] == 410 and b["check"]  # 14.6% de diferencia -> ⚠️
assert c.best_day({}) is None
print("ok")

# carros: se escoge el más barato que quepa
c.mcp_call = lambda *a: {"results": [
    {"name": "Kia Rio", "category": "Economy", "price": 210, "passengers": 5, "vendor": "Alamo"},
    {"name": "Toyota RAV4", "category": "SUV", "price": 260, "passengers": 5, "vendor": "Hertz"},
    {"name": "Chrysler Pacifica", "category": "Minivan", "price": 480, "passengers": 7, "vendor": "Avis"},
    {"name": "Ford Transit", "category": "Van", "price": 700, "passengers": 12, "vendor": "Budget"}]}
c.PAUSE = 0
r = c.cars("MCO", "2027-01-14", "2027-01-21")
assert r["small"]["price"] == 210 and r["van"]["price"] == 480, r
print("ok carros")
