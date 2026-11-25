from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from core.gallery_service import GalleryError, GalleryService


class GalleryManager(tk.Toplevel):
    def __init__(
        self,
        master,
        gallery_service: GalleryService,
        sites,
        on_changed=None,
        is_task_running=None,
    ) -> None:
        super().__init__(master)
        self.title("图库设置")
        self.geometry("760x520")
        self.minsize(640, 440)
        self.transient(master)
        self.gallery_service = gallery_service
        self.sites = sites
        self.on_changed = on_changed
        self.is_task_running = is_task_running or (lambda: False)
        self.name_var = tk.StringVar()
        self.file_var = tk.StringVar()
        self.message_var = tk.StringVar(
            value="文件格式：每行一个图片链接；需要按标题匹配时可先写 [分组名]"
        )
        self._build()
        self._reload()

    def _build(self) -> None:
        container = ttk.Frame(self, padding=14)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(container, text="图库设置", style="Metric.TLabel").pack(anchor="w")
        ttk.Label(container, textvariable=self.message_var, style="Muted.TLabel").pack(fill=tk.X, pady=(2, 10))

        panes = ttk.Panedwindow(container, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)
        left = ttk.LabelFrame(panes, text="图库列表", padding=8)
        right = ttk.LabelFrame(panes, text="图库信息", padding=10)
        panes.add(left, weight=1)
        panes.add(right, weight=2)

        self.tree = ttk.Treeview(left, columns=("count",), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="名称")
        self.tree.heading("count", text="图片")
        self.tree.column("#0", width=180)
        self.tree.column("count", width=55, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._select)

        ttk.Label(right, text="图库名称").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(right, textvariable=self.name_var).grid(row=0, column=1, sticky="ew", pady=6, ipady=3)
        ttk.Label(right, text="映射文件").grid(row=1, column=0, sticky="w", pady=6)
        file_row = ttk.Frame(right)
        file_row.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Entry(file_row, textvariable=self.file_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(file_row, text="选择文件", command=self._browse).pack(side=tk.LEFT, padx=(6, 0))
        right.columnconfigure(1, weight=1)

        actions = ttk.Frame(right)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="新增图库", command=self._create, style="Accent.TButton").pack(side=tk.LEFT)
        ttk.Button(actions, text="重命名", command=self._rename).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="删除", command=self._delete).pack(side=tk.LEFT)

        ttk.Button(container, text="关闭", command=self.destroy).pack(anchor="e", pady=(10, 0))

    def _reload(self, selected_id: str = "") -> None:
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        try:
            galleries = self.gallery_service.list_galleries()
        except GalleryError as exc:
            self.message_var.set(str(exc))
            return
        for gallery in galleries:
            validation = self.gallery_service.validate_gallery(gallery.gallery_id)
            self.tree.insert("", tk.END, iid=gallery.gallery_id, text=gallery.name, values=(validation.url_count,))
        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
            self.tree.focus(selected_id)
        if self.on_changed:
            self.on_changed()

    def _selected_id(self) -> str:
        selection = self.tree.selection()
        return selection[0] if selection else ""

    def _select(self, _event=None) -> None:
        gallery_id = self._selected_id()
        if not gallery_id:
            return
        try:
            gallery = self.gallery_service.require_gallery(gallery_id)
            validation = self.gallery_service.validate_gallery(gallery_id)
        except GalleryError as exc:
            self.message_var.set(str(exc))
            return
        self.name_var.set(gallery.name)
        self.file_var.set(str(gallery.file_path))
        self.message_var.set(f"已识别图片链接：{validation.url_count} 条；[分组名] 可选")

    def _browse(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="选择图库映射文件",
            initialdir=Path(self.gallery_service.settings_dir).parent,
            filetypes=(("文本文件", "*.txt"), ("所有文件", "*.*")),
        )
        if selected:
            self.file_var.set(selected)

    def _create(self) -> None:
        if self.is_task_running():
            self.message_var.set("发布任务运行中，暂不能新增图库")
            return
        try:
            gallery = self.gallery_service.create_gallery(self.name_var.get(), self.file_var.get())
        except GalleryError as exc:
            self.message_var.set(f"新增失败：{exc}")
            return
        self.message_var.set(f"已新增图库：{gallery.name}")
        self._reload(gallery.gallery_id)

    def _rename(self) -> None:
        if self.is_task_running():
            self.message_var.set("发布任务运行中，暂不能重命名图库")
            return
        gallery_id = self._selected_id()
        if not gallery_id:
            self.message_var.set("请先选择图库")
            return
        try:
            gallery = self.gallery_service.rename_gallery(gallery_id, self.name_var.get())
        except GalleryError as exc:
            self.message_var.set(f"重命名失败：{exc}")
            return
        self.message_var.set(f"已重命名为：{gallery.name}")
        self._reload(gallery_id)

    def _delete(self) -> None:
        if self.is_task_running():
            self.message_var.set("发布任务运行中，暂不能删除图库")
            return
        gallery_id = self._selected_id()
        if not gallery_id:
            self.message_var.set("请先选择图库")
            return
        referenced = {site.gallery_id for site in self.sites if site.gallery_id}
        try:
            self.gallery_service.delete_gallery(gallery_id, referenced_gallery_ids=referenced)
        except GalleryError as exc:
            self.message_var.set(f"删除失败：{exc}")
            return
        self.name_var.set("")
        self.file_var.set("")
        self.message_var.set("图库已删除")
        self._reload()
