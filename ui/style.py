from __future__ import annotations

from tkinter import ttk


def apply_console_style(root) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    root.configure(bg="#f8fafc")
    style.configure(".", font=("Microsoft YaHei UI", 9), background="#f8fafc", foreground="#0f172a")
    style.configure("TFrame", background="#f8fafc")
    style.configure("TLabelframe", background="#f8fafc", bordercolor="#cbd5e1", relief="solid")
    style.configure("TLabelframe.Label", background="#f8fafc", foreground="#0f172a", font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("TLabel", background="#f8fafc", foreground="#0f172a")
    style.configure("Muted.TLabel", background="#f8fafc", foreground="#475569", font=("Microsoft YaHei UI", 8))
    style.configure("Metric.TLabel", background="#f8fafc", foreground="#0f172a", font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("TButton", padding=(6, 3), foreground="#0f172a")
    style.map("TButton", background=[("active", "#e2e8f0"), ("pressed", "#cbd5e1")])
    style.configure("Accent.TButton", background="#22c55e", foreground="#052e16", padding=(8, 4))
    style.map("Accent.TButton", background=[("active", "#16a34a"), ("pressed", "#15803d")])
    style.configure("Compact.TButton", padding=(5, 2), foreground="#0f172a")
    style.configure("Toolbar.TNotebook", background="#f8fafc", borderwidth=0)
    style.configure("Toolbar.TNotebook.Tab", padding=(8, 4), font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("Treeview", rowheight=24, fieldbackground="#ffffff", background="#ffffff", foreground="#0f172a")
    style.configure("Treeview.Heading", background="#e2e8f0", foreground="#0f172a", font=("Microsoft YaHei UI", 9, "bold"))
    style.map("Treeview", background=[("selected", "#bfdbfe")], foreground=[("selected", "#0f172a")])
    style.configure("Horizontal.TProgressbar", troughcolor="#e2e8f0", background="#22c55e")
