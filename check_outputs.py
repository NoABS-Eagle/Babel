import openpyxl
from pathlib import Path

# xlsx
wb = openpyxl.load_workbook("outputs/eagle_exp.xlsx")
ws = wb.active
print("=== xlsx (Components sheet) ===")
for row in ws.iter_rows(values_only=True):
    print("  " + " | ".join(str(v or "") for v in row))

# DbLib
print("\n=== eagle_exp.DbLib ===")
print(Path("outputs/eagle_exp.DbLib").read_text(encoding="utf-8"))
