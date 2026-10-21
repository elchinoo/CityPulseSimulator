"""The request types TrustDesk understands, mirrored here so the simulator can validate a plan offline.

If the target knows fewer types (an older TrustDesk), the run reports those requests as invalid; it never guesses.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RType:
    key: str
    label: str
    kind: str                # leave | travel | expense | other


TYPES: dict[str, RType] = {t.key: t for t in (
    RType("vacation", "Vacation / annual leave", "leave"),
    RType("sick_leave", "Sick leave", "leave"),
    RType("parental_leave", "Parental leave", "leave"),
    RType("bereavement_leave", "Bereavement leave", "leave"),
    RType("unpaid_leave", "Unpaid leave", "leave"),
    RType("business_trip", "Business trip", "travel"),
    RType("expense_travel", "Expense: travel", "expense"),
    RType("expense_customer", "Expense: customer", "expense"),
    RType("expense_meals", "Expense: meals", "expense"),
    RType("expense_medical", "Expense: medical / sick-related", "expense"),
    RType("expense_training", "Expense: training", "expense"),
    RType("expense_equipment", "Expense: equipment", "expense"),
    RType("expense_home_office", "Expense: home office", "expense"),
    RType("expense_other", "Expense: other", "expense"),
    RType("remote_work", "Remote work", "other"),
    RType("overtime", "Overtime", "other"),
    RType("training_request", "Training or conference", "other"),
    RType("equipment_request", "Equipment", "other"),
    RType("other", "Other request", "other"),
)}

DEPARTMENTS = (("Engineering", 30), ("Sales", 18), ("Support", 16), ("Finance", 8), ("Marketing", 8),
               ("Operations", 8), ("HR", 4), ("Legal", 3), ("Product", 5))
COUNTRIES = (("US", 40), ("DE", 8), ("GB", 8), ("SE", 4), ("JP", 4), ("IT", 5), ("PT", 4), ("BR", 5), ("IN", 12), ("CA", 6))
FIRST = ("Ada", "Ben", "Chloe", "Dev", "Elena", "Femi", "Gus", "Hana", "Ivan", "Jade", "Kofi", "Lara", "Mateo", "Nina",
         "Omar", "Priya", "Quinn", "Rosa", "Sam", "Tara", "Uma", "Viktor", "Wen", "Xena", "Yusuf", "Zoe", "Amir", "Bea",
         "Cal", "Dara", "Eli", "Fay", "Gia", "Hugo", "Ines", "Jon", "Kai", "Lena", "Milo", "Noor")
LAST = ("Alvarez", "Bauer", "Chen", "Dubois", "Eriksson", "Fischer", "Garcia", "Haddad", "Ivanov", "Jensen", "Khan",
        "Lopez", "Meyer", "Novak", "Okafor", "Patel", "Quist", "Rossi", "Silva", "Tanaka", "Umar", "Vega", "Weber",
        "Xu", "Yilmaz", "Zhou", "Moreau", "Nilsson", "Oliveira", "Petrov", "Rahman", "Santos", "Takahashi", "Walker")
TITLES = {"Engineering": "Software Engineer", "Sales": "Account Executive", "Support": "Support Engineer",
          "Finance": "Financial Analyst", "Marketing": "Marketing Specialist", "Operations": "Operations Analyst",
          "HR": "HR Partner", "Legal": "Legal Counsel", "Product": "Product Manager"}
