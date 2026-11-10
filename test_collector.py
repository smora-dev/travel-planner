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
