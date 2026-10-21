"""Random but reproducible employees and requests. Pure functions: no network, no clock except the `today` argument.

Each request is "ok" (written to satisfy a typical company policy) or a "problem" (built to break one: too little
notice, over a limit, no receipt, overlapping leave, a duplicate...). The problem share is the company's problem_pct.
The text and numbers assume common policy values; nothing here is TrustDesk's rulebook, so a different policy set will
judge the same request differently, which is the point of testing it.
"""
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from .hr_catalog import COUNTRIES, DEPARTMENTS, FIRST, LAST, TITLES, TYPES
from .hr_model import Company

LEAVE_PICK_TENURED = 0.7


@dataclass
class Employee:
    employee_no: str
    name: str
    email: str
    department: str
    title: str
    level: str
    country: str
    hire_date: str
    manager_no: str | None = None
    existing: bool = False        # already in TrustDesk (found at run time), so never created again

    def tenure_days(self, today: date) -> int:
        return (today - date.fromisoformat(self.hire_date)).days

    def payload(self) -> dict:
        d = {"employee_no": self.employee_no, "name": self.name, "email": self.email, "department": self.department,
             "title": self.title, "level": self.level, "country": self.country, "hire_date": self.hire_date}
        if self.manager_no:
            d["manager_no"] = self.manager_no
        return d


@dataclass
class Item:
    index: int
    type: str
    problem: bool
    withdraw: bool
    payload: dict            # the POST /api/v1/requests body


@dataclass
class Plan:
    seed: int
    employees: list[Employee]
    items: list[Item]
    counts: dict[str, int] = field(default_factory=dict)

    def problems(self) -> int:
        return sum(1 for i in self.items if i.problem)


def _weighted(rng: random.Random, pairs):
    return rng.choices([p[0] for p in pairs], [p[1] for p in pairs])[0]


def make_employees(c: Company, seed: int, rng: random.Random, today: date) -> list[Employee]:
    out: list[Employee] = []
    tag = f"{c.employee_prefix}{seed % 100000:05d}"
    used: set[str] = set()
    n_total = c.new_employees + c.tenured_employees
    for i in range(n_total):
        tenured = i < c.tenured_employees
        first, last = rng.choice(FIRST), rng.choice(LAST)
        dept = _weighted(rng, DEPARTMENTS)
        no = f"{tag}-{i + 1:04d}"
        email = f"{first}.{last}.{tag}.{i + 1}@example.com".lower()
        level = rng.choice(["L3", "L4", "L5", "L6"] if tenured and i % 6 == 0 else ["L1", "L2", "L3", "L3", "L4"])
        days = rng.randint(400, 5500) if tenured else rng.randint(3, 75)
        used.add(no)
        out.append(Employee(no, f"{first} {last}", email, dept, TITLES[dept], level, _weighted(rng, COUNTRIES),
                            (today - timedelta(days=days)).isoformat()))
    # managers: the first tenured employees of each department manage the rest of it
    by_dept: dict[str, list[Employee]] = {}
    for e in out:
        by_dept.setdefault(e.department, []).append(e)
    for members in by_dept.values():
        tenured_first = sorted(members, key=lambda e: -e.tenure_days(today))
        managers = tenured_first[: max(1, len(members) // 8)]
        for m in managers:
            m.level, m.title = "L6", f"{m.department} Manager"
        for e in members:
            if e not in managers:
                e.manager_no = rng.choice(managers).employee_no
    out.sort(key=lambda e: e.manager_no is not None)   # managers first, so manager_no always resolves
    return out


# ---- date helpers ---------------------------------------------------------------------------------------
def _monday_on_or_after(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7)


def _workdays_end(start: date, days: int) -> date:
    """Last day of a leave that covers `days` working days starting on `start` (a weekday)."""
    end, left = start, days
    while True:
        if end.weekday() < 5:
            left -= 1
            if left == 0:
                return end
        end += timedelta(days=1)


@dataclass
class Ctx:
    rng: random.Random
    today: date
    windows: dict[str, list[tuple[date, date]]] = field(default_factory=dict)
    expenses: list[tuple[str, dict]] = field(default_factory=list)

    def free_window(self, emp: Employee, workdays: int, earliest: int, latest: int) -> tuple[date, date]:
        for _ in range(12):
            start = _monday_on_or_after(self.today + timedelta(days=self.rng.randint(earliest, latest)))
            end = _workdays_end(start, workdays)
            if all(end < s or start > e for s, e in self.windows.get(emp.employee_no, [])):
                return self._take(emp, start, end)
        return self._take(emp, start, end)

    def _take(self, emp, start, end):
        self.windows.setdefault(emp.employee_no, []).append((start, end))
        return start, end


def _money(rng, lo, hi) -> float:
    return round(rng.uniform(lo, hi), 2)


def _iso(d: date) -> str:
    return d.isoformat()


def _expense(ctx, emp, type_, title, desc, amount, details, days_ago, problem, currency="USD"):
    when = ctx.today - timedelta(days=days_ago)
    body = {"employee_no": emp.employee_no, "type": type_, "title": title, "description": desc,
            "start_date": _iso(when), "amount": amount, "currency": currency, "details": details}
    ctx.expenses.append((type_, body))
    return body


# ---- one generator per type; each returns (payload, problem_really_applied) ----------------------------------
def g_vacation(ctx, emp, problem):
    rng = ctx.rng
    kind = rng.choice(["short_notice", "too_long", "overlap", "retro"]) if problem else None
    prior = ctx.windows.get(emp.employee_no)
    if kind == "overlap" and prior:
        s0, e0 = rng.choice(prior)
        start, end = s0 + timedelta(days=rng.randint(0, 2)), e0 + timedelta(days=rng.randint(1, 4))
    elif kind == "short_notice":
        start = _monday_on_or_after(ctx.today + timedelta(days=rng.randint(2, 9)))
        end = _workdays_end(start, rng.randint(11, 16))
        ctx.windows.setdefault(emp.employee_no, []).append((start, end))
    elif kind == "too_long":
        start = _monday_on_or_after(ctx.today + timedelta(days=rng.randint(45, 120)))
        end = _workdays_end(start, rng.randint(16, 24))
        ctx.windows.setdefault(emp.employee_no, []).append((start, end))
    elif kind == "retro":
        start = ctx.today - timedelta(days=rng.randint(2, 6))
        end = start + timedelta(days=rng.randint(2, 4))
        ctx.windows.setdefault(emp.employee_no, []).append((start, end))
    else:
        kind = None
        start, end = ctx.free_window(emp, rng.choice([2, 3, 4, 5, 5, 5, 8, 10]), 35, 240)
    why = rng.choice(["a family trip", "a week at the coast", "visiting relatives", "a long weekend in the mountains",
                      "recharging before the next release", "a wedding abroad", "moving house"])
    return {"employee_no": emp.employee_no, "type": "vacation", "title": rng.choice(["Vacation", "Annual leave", "Time off"]),
            "description": f"Time off for {why}.", "start_date": _iso(start), "end_date": _iso(end)}, kind is not None


def g_sick(ctx, emp, problem):
    rng = ctx.rng
    kind = rng.choice(["no_certificate", "very_long", "late"]) if problem else None
    if kind == "no_certificate":
        days, ago, cert = rng.randint(4, 9), rng.randint(2, 5), False
    elif kind == "very_long":
        days, ago, cert = rng.randint(11, 16), rng.randint(1, 3), True
    elif kind == "late":
        days, ago, cert = rng.randint(1, 3), rng.randint(14, 25), None
    else:
        days, ago, cert = rng.randint(1, 3), rng.randint(0, 3), rng.choice([None, True])
    start = ctx.today - timedelta(days=ago + days)
    end = start + timedelta(days=days - 1)
    details = {} if cert is None else {"medical_certificate": cert}
    what = rng.choice(["the flu", "a stomach bug", "a migraine", "back pain", "a fever", "a bad cold", "an injury"])
    return {"employee_no": emp.employee_no, "type": "sick_leave", "title": "Sick leave",
            "description": f"Off sick with {what}.", "start_date": _iso(start), "end_date": _iso(end),
            "details": details}, kind is not None


def g_parental(ctx, emp, problem):
    rng = ctx.rng
    ptype = rng.choice(["maternity", "paternity", "adoption"])
    weeks = 16 if ptype == "maternity" else 8
    kind = rng.choice(["short_notice", "too_long"]) if problem else None
    if kind == "short_notice":
        start = ctx.today + timedelta(days=rng.randint(2, 12))
    else:
        start = ctx.today + timedelta(days=rng.randint(45, 150))
    if kind == "too_long":
        weeks = 16 if ptype != "maternity" else 30
    end = start + timedelta(weeks=weeks) - timedelta(days=1)
    return {"employee_no": emp.employee_no, "type": "parental_leave", "title": f"{ptype.title()} leave",
            "description": f"{ptype.title()} leave of {weeks} weeks.", "start_date": _iso(start), "end_date": _iso(end),
            "details": {"parental_type": ptype}}, kind is not None


def g_bereavement(ctx, emp, problem):
    rng = ctx.rng
    rel, days = rng.choice([("spouse", 5), ("parent", 5), ("sibling", 5), ("child", 5), ("grandparent", 3),
                            ("parent-in-law", 3), ("close friend", 1)])
    if problem:
        rel, days = rng.choice([("cousin", 8), ("neighbour", 5), ("grandparent", 10)])
    start = ctx.today - timedelta(days=rng.randint(0, 6))
    return {"employee_no": emp.employee_no, "type": "bereavement_leave", "title": "Bereavement leave",
            "description": f"Time off after the death of a {rel}.", "start_date": _iso(start),
            "end_date": _iso(_workdays_end(start if start.weekday() < 5 else _monday_on_or_after(start), days)),
            "details": {"relationship": rel}}, problem


def g_unpaid(ctx, emp, problem):
    rng = ctx.rng
    days = rng.randint(120, 200) if problem else rng.randint(7, 60)
    start = ctx.today + timedelta(days=rng.randint(3, 20) if problem else rng.randint(40, 150))
    return {"employee_no": emp.employee_no, "type": "unpaid_leave", "title": "Unpaid leave",
            "description": rng.choice(["Extended trip to care for a relative.", "Personal sabbatical.", "Study leave."]),
            "start_date": _iso(start), "end_date": _iso(start + timedelta(days=days))}, problem


CITIES = [("Chicago", 0.9), ("Austin", 0.9), ("Berlin", 1.0), ("Lisbon", 0.8), ("London", 1.3), ("Tokyo", 1.5),
          ("Zurich", 1.5), ("Toronto", 1.0), ("Stockholm", 1.1), ("Madrid", 0.9)]


def g_trip(ctx, emp, problem):
    rng = ctx.rng
    city, factor = rng.choice(CITIES)
    days = rng.randint(2, 5)
    cost = _money(rng, 700, 2600) * factor
    kind = rng.choice(["long", "expensive", "business_class"]) if problem else None
    if kind == "long":
        days = rng.randint(16, 25)
    elif kind == "expensive":
        cost = _money(rng, 11000, 18000)
    start, end = ctx.free_window(emp, days, 21 if not problem else 8, 120)
    end = start + timedelta(days=days - 1) if kind == "long" else end
    desc = f"Trip to {city} for {rng.choice(['a customer workshop', 'a partner meeting', 'a conference', 'an on-site audit'])}."
    if kind == "business_class":
        desc += " Booking business class for the long flight."
        cost = _money(rng, 4500, 7000)
    return {"employee_no": emp.employee_no, "type": "business_trip", "title": f"Trip to {city}", "description": desc,
            "start_date": _iso(start), "end_date": _iso(end),
            "details": {"destination": city, "purpose": "Customer or partner work", "transport": "flight",
                        "estimated_cost": round(cost, 2)}}, kind is not None


def g_expense_travel(ctx, emp, problem):
    rng = ctx.rng
    cat = rng.choice(["flight", "train", "taxi", "lodging", "mileage", "lodging"])
    lo, hi = {"flight": (220, 720), "train": (35, 210), "taxi": (14, 75), "lodging": (110, 195),
              "mileage": (18, 130)}[cat]
    amount, ago, receipt, kind = _money(rng, lo, hi), rng.randint(2, 40), True, None
    if problem:
        kind = rng.choice(["over_cap", "no_receipt", "too_old", "future", "duplicate"] if cat == "lodging" else
                          ["no_receipt", "too_old", "future", "duplicate"])
        if kind == "over_cap":
            amount = _money(rng, 330, 620)
        elif kind == "no_receipt":
            receipt = False
            amount = max(amount, 40)
        elif kind == "too_old":
            ago = rng.randint(75, 140)
        elif kind == "future":
            ago = -rng.randint(3, 20)
    prior = [b for t, b in ctx.expenses if t == "expense_travel" and b["employee_no"] == emp.employee_no]
    if kind == "duplicate" and prior:
        body = dict(rng.choice(prior))
        ctx.expenses.append(("expense_travel", body))
        return body, True
    merchant = rng.choice(["Lakeview Hotel", "City Rail", "Skyline Air", "Metro Cab", "Harbor Inn", "Parkside Suites"])
    body = _expense(ctx, emp, "expense_travel", f"{cat.title()} expense", f"{cat.title()} costs for a business trip ({merchant}).",
                    amount, {"category": cat, "merchant": merchant, "has_receipt": receipt}, ago, problem)
    return body, kind is not None


def g_expense_customer(ctx, emp, problem):
    rng = ctx.rng
    people = rng.randint(2, 6)
    per = _money(rng, 35, 92)
    customer = rng.choice(["Contoso", "Fabrikam", "Northwind", "Adventure Works", "Tailspin", "Wingtip"])
    purpose, receipt, kind, title = "Contract discussion", True, None, f"Dinner with {customer}"
    amount = round(per * people, 2)
    if problem:
        kind = rng.choice(["per_person", "gift_card", "no_receipt", "public_sector"])
        if kind == "per_person":
            amount = round(_money(rng, 135, 240) * people, 2)
        elif kind == "gift_card":
            amount, title, purpose = rng.choice([50, 75, 100, 150]), f"Gift card for {customer}", "Thank-you gift card"
        elif kind == "no_receipt":
            receipt = False
        else:
            customer, purpose, title = "City Transport Authority", "Hospitality for a government tender", "Lunch for officials"
    desc = {"gift_card": f"A gift card for the buyer at {customer} as a thank-you.",
            "public_sector": "Hospitality for civil servants during an open tender."}.get(
        kind, f"Meal with {people} people from {customer} to talk about the contract.")
    body = _expense(ctx, emp, "expense_customer", title, desc, amount,
                    {"customer": customer, "attendees": people, "business_purpose": purpose, "has_receipt": receipt},
                    rng.randint(1, 30), problem)
    return body, kind is not None


def g_expense_meals(ctx, emp, problem):
    rng = ctx.rng
    amount = _money(rng, 11, 68)
    kind, receipt = None, True
    if problem:
        kind = rng.choice(["too_high", "no_receipt", "duplicate"])
        amount = _money(rng, 150, 320) if kind == "too_high" else max(amount, 45)
        receipt = kind != "no_receipt"
    prior = [b for t, b in ctx.expenses if t == "expense_meals" and b["employee_no"] == emp.employee_no]
    if kind == "duplicate" and prior:
        body = dict(rng.choice(prior))
        ctx.expenses.append(("expense_meals", body))
        return body, True
    body = _expense(ctx, emp, "expense_meals", "Business meal", "Lunch during a working session.", amount,
                    {"merchant": rng.choice(["Bistro Roma", "Noodle Bar", "The Grill", "Cafe Central"]),
                     "attendees": rng.randint(1, 4), "has_receipt": receipt}, rng.randint(1, 35), problem)
    return body, kind is not None


def g_expense_medical(ctx, emp, problem):
    rng = ctx.rng
    what, amount = (rng.choice([("Eye examination", 90), ("Work-travel vaccination", 70)]) if not problem
                    else rng.choice([("Dental treatment", 420), ("Physiotherapy", 260), ("Prescription glasses", 380)]))
    body = _expense(ctx, emp, "expense_medical", what, f"{what} paid out of pocket.", round(amount + _money(rng, 0, 40), 2),
                    {"provider": rng.choice(["City Clinic", "Vision Plus", "Wellness Centre"]), "has_receipt": True},
                    rng.randint(3, 40), problem)
    return body, problem


def g_expense_training(ctx, emp, problem):
    rng = ctx.rng
    amount = _money(rng, 2200, 4800) if problem else _money(rng, 60, 900)
    course = rng.choice(["Advanced SQL", "Cloud architecture", "Public speaking", "Leadership basics", "Security awareness"])
    body = _expense(ctx, emp, "expense_training", f"Course: {course}", f"Fee for the {course} course.", amount,
                    {"course": course, "provider": "LearnCo", "has_receipt": True}, rng.randint(2, 40), problem)
    return body, problem


def g_expense_equipment(ctx, emp, problem):
    rng = ctx.rng
    item, amount = (rng.choice([("Laptop", 1900), ("4K monitor", 1300), ("Standing desk", 1150)]) if problem
                    else rng.choice([("USB hub", 35), ("Headset", 110), ("Keyboard", 90), ("Webcam", 70)]))
    body = _expense(ctx, emp, "expense_equipment", item, f"{item} bought for work.", round(amount + _money(rng, 0, 60), 2),
                    {"item": item, "merchant": "TechShop", "has_receipt": True}, rng.randint(2, 30), problem)
    return body, problem


def g_expense_home(ctx, emp, problem):
    rng = ctx.rng
    item = rng.choice(["Desk and chair", "Monitor arm", "Desk lamp", "Ergonomic mouse"])
    amount = _money(rng, 650, 1300) if problem else _money(rng, 30, 420)
    body = _expense(ctx, emp, "expense_home_office", item, f"Home office: {item.lower()}.", amount,
                    {"item": item, "has_receipt": True}, rng.randint(2, 40), problem)
    return body, problem


def g_expense_other(ctx, emp, problem):
    rng = ctx.rng
    amount, receipt, what = _money(rng, 8, 24), True, rng.choice(["Parking", "Printing", "Postage", "Notebook"])
    if problem:
        what, amount, receipt = rng.choice(["Phone case", "Gym pass", "Traffic fine", "Spouse ticket"]), _money(rng, 30, 140), False
    body = _expense(ctx, emp, "expense_other", what, f"{what} expense.", amount,
                    {"merchant": "Local shop", "category_note": what, "has_receipt": receipt}, rng.randint(1, 30), problem)
    return body, problem


def g_remote(ctx, emp, problem):
    rng = ctx.rng
    if problem:
        loc, country = rng.choice([("Lisbon", "PT"), ("Bali", "ID"), ("Dubai", "AE"), ("Mexico City", "MX")])
        days = rng.randint(22, 35)
        start = ctx.today + timedelta(days=rng.randint(5, 14))
    else:
        loc, country, days = rng.choice(["Home", "Co-working space"]), emp.country, rng.randint(5, 10)
        start = _monday_on_or_after(ctx.today + timedelta(days=rng.randint(14, 90)))
    return {"employee_no": emp.employee_no, "type": "remote_work", "title": f"Remote work from {loc}",
            "description": f"Working remotely from {loc} for {days} days.", "start_date": _iso(start),
            "end_date": _iso(start + timedelta(days=days - 1)), "details": {"location": loc, "country": country}}, problem


def g_overtime(ctx, emp, problem):
    rng = ctx.rng
    hours = rng.randint(13, 22) if problem else rng.randint(1, 6)
    return {"employee_no": emp.employee_no, "type": "overtime", "title": "Overtime",
            "description": rng.choice(["Release night work.", "Customer incident.", "Month-end close."]),
            "start_date": _iso(ctx.today - timedelta(days=rng.randint(1, 5))), "details": {"hours": hours}}, problem


def g_training(ctx, emp, problem):
    rng = ctx.rng
    cost = _money(rng, 3500, 6500) if problem else _money(rng, 150, 1300)
    course = rng.choice(["DevOps summit", "Data conference", "Negotiation workshop", "Kubernetes training"])
    start = ctx.today + timedelta(days=rng.randint(25, 120))
    return {"employee_no": emp.employee_no, "type": "training_request", "title": course,
            "description": f"Request to attend {course}.", "start_date": _iso(start),
            "end_date": _iso(start + timedelta(days=rng.randint(1, 4))),
            "details": {"course": course, "provider": "EventCo", "estimated_cost": cost}}, problem


def g_equipment(ctx, emp, problem):
    rng = ctx.rng
    item, cost = (rng.choice([("Workstation", 3200), ("Dual monitor setup", 1800)]) if problem
                  else rng.choice([("Laptop stand", 60), ("Docking station", 190), ("Noise-cancelling headset", 250)]))
    return {"employee_no": emp.employee_no, "type": "equipment_request", "title": f"Request: {item}",
            "description": f"I need a {item.lower()} for my daily work.", "details": {"item": item, "estimated_cost": cost}}, problem


def g_other(ctx, emp, problem):
    rng = ctx.rng
    label, desc = rng.choice([
        ("Vacation carry-over exception", "Please let me carry unused vacation days into next year."),
        ("Flexible hours", "I would like to start and finish two hours earlier."),
        ("Relocation question", "Could the company support a move to another city?"),
        ("Policy exception", "Requesting an exception to the standard notice period.")])
    if problem:
        label, desc = "Carry over 15 vacation days", "Please let me carry 15 unused vacation days into next year."
    return {"employee_no": emp.employee_no, "type": "other", "title": label, "description": desc,
            "details": {"custom_type": label}}, problem


GENERATORS = {
    "vacation": g_vacation, "sick_leave": g_sick, "parental_leave": g_parental, "bereavement_leave": g_bereavement,
    "unpaid_leave": g_unpaid, "business_trip": g_trip, "expense_travel": g_expense_travel,
    "expense_customer": g_expense_customer, "expense_meals": g_expense_meals, "expense_medical": g_expense_medical,
    "expense_training": g_expense_training, "expense_equipment": g_expense_equipment,
    "expense_home_office": g_expense_home, "expense_other": g_expense_other, "remote_work": g_remote,
    "overtime": g_overtime, "training_request": g_training, "equipment_request": g_equipment, "other": g_other,
}
assert set(GENERATORS) == set(TYPES)


def build_plan(c: Company, today: date | None = None, existing: list[Employee] | None = None) -> Plan:
    """The whole run, decided up front from the seed. `existing` are employees found in TrustDesk (use_existing)."""
    today = today or date.today()
    seed = c.seed if c.seed is not None else random.SystemRandom().randrange(2**32)
    rng = random.Random(seed)
    generated = make_employees(c, seed, rng, today)
    pool = generated + [e for e in (existing or []) if c.use_existing]
    items: list[Item] = []
    if not pool:
        return Plan(seed, generated, items, dict(c.requests))
    tenured = [e for e in pool if e.tenure_days(today) >= 365]
    fresh = [e for e in pool if e.tenure_days(today) < 365]
    ctx = Ctx(rng, today)
    for type_, n in sorted(c.requests.items()):
        for _ in range(n):
            if tenured and fresh:
                pick_from = tenured if rng.random() < LEAVE_PICK_TENURED else fresh
            else:
                pick_from = tenured or fresh
            emp = rng.choice(pick_from)
            problem = rng.random() * 100 < c.problem_pct
            payload, applied = GENERATORS[type_](ctx, emp, problem)
            items.append(Item(0, type_, applied, rng.random() * 100 < c.withdraw_pct, payload))
    rng.shuffle(items)
    for i, it in enumerate(items):
        it.index = i
    return Plan(seed, generated, items, dict(c.requests))
