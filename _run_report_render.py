from pathlib import Path


source = Path(
    r"C:\Users\Fletcher\.codex\visualizations\2026\07\15\019f6645-db13-7ed3-be21-45c0928371a5"
    r"\codex-presentations\ecfc-minneapolis-report\tmp\analyze_and_render.py"
)
code = source.read_text(encoding="utf-8")
for old, new in {
    '"attack left"': '"attack_left"',
    '"attack center"': '"attack_center"',
    '"attack right"': '"attack_right"',
    '"left cross"': '"left_cross"',
    '"right cross"': '"right_cross"',
    '"left cross success"': '"left_cross_success"',
    '"right cross success"': '"right_cross_success"',
    '"left corner"': '"left_corner"',
    '"right corner"': '"right_corner"',
    '"aerial duel won"': '"aerial_duel_won"',
}.items():
    code = code.replace(old, new)
exec(compile(code, str(source), "exec"), {"__name__": "__main__"})
