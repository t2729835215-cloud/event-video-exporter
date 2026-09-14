import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


ROOT = Path(__file__).resolve().parent


def worker(source, output, fps, outputs):
    def emit(percent, message):
        print(json.dumps({"progress": percent, "message": message}, ensure_ascii=False), flush=True)

    try:
        from aedat4_joint_video import convert
        report = convert(source, output, fps, emit, outputs)
        print(json.dumps({"done": True, "report": report}, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), flush=True)
        traceback.print_exc()
        return 1


class ExportAssistant:
    def __init__(self, root):
        self.root = root
        self.root.title("事件与灰度视频导出助手")
        self.root.geometry("1060x700")
        self.root.minsize(880, 600)
        self.entries = {}
        self.active = False
        self.stop_after_current = threading.Event()
        self.messages = queue.Queue()
        self.output = tk.StringVar(value=str(ROOT / "事件视频"))
        self.fps = tk.StringVar(value="30")
        self.status = tk.StringVar(value="请选择已停止录制的 AEDAT4 文件。原始文件不会修改。")
        self.progress = tk.DoubleVar()
        style = ttk.Style(root)
        style.theme_use("vista" if "vista" in style.theme_names() else "default")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("Treeview", rowheight=30)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        outer = ttk.Frame(root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="事件与灰度视频导出助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="手动批量导出 · 三路 MP4 同一播放时间轴 · 不控制相机", foreground="#555555").pack(anchor="w", pady=(4, 16))
        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        self.add_button = ttk.Button(controls, text="选择录制文件（可多选）", command=self.choose_files)
        self.add_button.pack(side="left")
        self.remove_button = ttk.Button(controls, text="移除选中", command=self.remove_selected)
        self.remove_button.pack(side="left", padx=8)
        ttk.Label(controls, text="输出帧率").pack(side="left", padx=(18, 6))
        self.fps_box = ttk.Combobox(controls, textvariable=self.fps, values=(15, 25, 30, 50, 60, 100, 120, 200, 250, 500), width=6)
        self.fps_box.pack(side="left")
        ttk.Label(controls, text="fps（1～500）").pack(side="left", padx=6)
        destination = ttk.Frame(outer)
        destination.pack(fill="x", pady=12)
        ttk.Label(destination, text="输出目录").pack(side="left", padx=(0, 8))
        self.output_entry = ttk.Entry(destination, textvariable=self.output)
        self.output_entry.pack(side="left", fill="x", expand=True)
        self.directory_button = ttk.Button(destination, text="选择目录", command=self.choose_directory)
        self.directory_button.pack(side="left", padx=(8, 0))
        formats = ttk.Frame(outer)
        formats.pack(fill="x", pady=(0, 10))
        ttk.Label(formats, text="输出类型").pack(side="left", padx=(0, 10))
        self.types = {kind: tk.BooleanVar(value=True) for kind in ("joint", "events", "gray")}
        self.type_buttons = []
        for kind, label in [("joint", "联合视频"), ("events", "红绿事件视频"), ("gray", "灰度视频")]:
            button = ttk.Checkbutton(formats, text=label, variable=self.types[kind])
            button.pack(side="left", padx=(0, 16))
            self.type_buttons.append(button)
        listing = ttk.Frame(outer)
        listing.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=("file", "size", "state"), show="headings", selectmode="extended", height=5)
        for name, title, width in [("file", "录制文件", 490), ("size", "大小", 90), ("state", "处理状态", 280)]:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=70, stretch=name != "size")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.show_selection)
        ttk.Label(outer, text="输出：联合视频 / 红绿事件视频 / 灰度视频 / 时间对应表 / 预览 / 报告", foreground="#444444").pack(anchor="w", pady=(12, 3))
        ttk.Label(outer, text="本助手需录制内同时有事件和灰度；高FPS不增加灰度采样率。使用系统临时磁盘缓存，不足会报错。", foreground="#72551c").pack(anchor="w", pady=(0, 8))
        ttk.Progressbar(outer, variable=self.progress, maximum=100).pack(fill="x")
        ttk.Label(outer, textvariable=self.status, wraplength=970).pack(anchor="w", pady=8)
        self.details = tk.Text(outer, height=4, wrap="word", font=("Microsoft YaHei UI", 9), state="disabled")
        self.details.pack(fill="x", pady=(0, 10))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="开始导出 / 重试失败项", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="取消后续任务", command=self.cancel_pending, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="打开选中结果 / 输出目录", command=self.open_output).pack(side="right")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)

    def choose_files(self):
        self.add_files(filedialog.askopenfilenames(title="选择已经停止录制的文件", initialdir=str(ROOT / "事件数据流"), filetypes=[("AEDAT4录制", "*.aedat4")]))

    def add_files(self, paths):
        if self.active:
            return
        existing = {str(e["source"]).casefold() for e in self.entries.values()}
        for value in paths:
            path = Path(value).resolve()
            if str(path).casefold() in existing:
                continue
            if not path.is_file() or path.suffix.lower() != ".aedat4":
                self.status.set(f"跳过非 AEDAT4 文件：{path.name}")
                continue
            key = self.tree.insert("", "end", values=(path.name, f"{path.stat().st_size / 1024 ** 2:.1f} MB", "等待"))
            self.entries[key] = {"source": path, "state": "等待", "output": None, "detail": str(path)}
            existing.add(str(path).casefold())

    def remove_selected(self):
        if not self.active:
            for key in self.tree.selection():
                self.tree.delete(key)
                del self.entries[key]

    def choose_directory(self):
        selected = filedialog.askdirectory(title="选择输出目录", initialdir=self.output.get())
        if selected:
            self.output.set(selected)

    def show_selection(self, event=None):
        selected = self.tree.selection()
        if selected:
            self.details.configure(state="normal")
            self.details.delete("1.0", "end")
            self.details.insert("end", self.entries[selected[0]]["detail"])
            self.details.configure(state="disabled")

    def set_busy(self, active):
        self.active = active
        for widget in (self.add_button, self.remove_button, self.fps_box, self.output_entry, self.directory_button, self.start_button, *self.type_buttons):
            widget.configure(state="disabled" if active else "normal")
        self.cancel_button.configure(state="normal" if active else "disabled")

    def start(self):
        if self.active:
            return
        jobs = [(key, entry["source"]) for key, entry in self.entries.items() if entry["state"] != "完成"]
        if not jobs:
            messagebox.showinfo("没有待处理文件", "请先添加文件；已完成的文件不会重复导出。")
            return
        try:
            outputs = [kind for kind, selected in self.types.items() if selected.get()]
            if not outputs:
                raise ValueError("请至少勾选一种输出视频")
            fps = int(self.fps.get())
            if not 1 <= fps <= 500:
                raise ValueError("帧率必须在1到500之间")
            if not self.output.get().strip():
                raise ValueError("请选择输出目录")
            destination = Path(self.output.get()).expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as error:
            messagebox.showerror("参数错误", str(error))
            return
        self.stop_after_current.clear()
        self.progress.set(0)
        self.set_busy(True)
        for key, _ in jobs:
            self.update_entry(key, "等待", "已排队，等待导出")
        threading.Thread(target=self.run_queue, args=(jobs, destination, fps, outputs), daemon=True).start()

    def run_queue(self, jobs, destination, fps, outputs):
        succeeded, failed = 0, 0
        for position, (key, source) in enumerate(jobs, 1):
            if self.stop_after_current.is_set():
                break
            output = None
            try:
                base = f"{source.stem} 导出 {datetime.now():%Y%m%d %H%M%S}"
                output = destination / base
                suffix = 2
                while output.exists():
                    output = destination / f"{base} {suffix}"
                    suffix += 1
                self.messages.put(("begin", key, output, position, len(jobs)))
                executable = Path(sys.executable).with_name("python.exe") if os.name == "nt" else Path(sys.executable)
                env = dict(os.environ, PYTHONIOENCODING="utf-8")
                command = [str(executable), "-u", str(Path(__file__).resolve()), "--worker", str(source), "--output", str(output), "--fps", str(fps), "--types", *outputs]
                lines, done, error_message = [], False, ""
                with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
                    for line in process.stdout:
                        lines.append(line.rstrip())
                        lines = lines[-60:]
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if "progress" in data:
                            self.messages.put(("progress", key, data["progress"], data["message"]))
                        if "error" in data:
                            error_message = data["error"]
                        done = done or data.get("done", False)
                    code = process.wait()
                if code or not done or not (output / "06 转换结果.json").is_file():
                    raise RuntimeError(error_message or "转换进程未正常完成\n" + "\n".join(lines[-12:]))
                succeeded += 1
                self.messages.put(("result", key, "完成", str(output)))
            except Exception as error:
                failed += 1
                detail = f"{source}\n{error}\n已有的不完整输出不会自动删除：{output}"
                self.messages.put(("result", key, "失败", detail))
        self.messages.put(("finished", succeeded, failed, self.stop_after_current.is_set()))

    def update_entry(self, key, state, detail):
        self.entries[key].update(state=state, detail=detail)
        self.tree.set(key, "state", state)
        if key in self.tree.selection():
            self.show_selection()

    def poll(self):
        try:
            while True:
                message = self.messages.get_nowait()
                kind = message[0]
                if kind == "begin":
                    _, key, output, position, total = message
                    self.entries[key]["output"] = output
                    self.tree.selection_set(key)
                    self.tree.see(key)
                    self.update_entry(key, "处理中", f"输出位置：{output}")
                    self.progress.set(0)
                    self.status.set(f"正在处理第 {position}/{total} 个文件")
                elif kind == "progress":
                    _, key, percent, text = message
                    self.progress.set(percent)
                    self.tree.set(key, "state", text)
                    self.status.set(f"{self.entries[key]['source'].name}：{text}")
                elif kind == "result":
                    self.update_entry(*message[1:])
                elif kind == "finished":
                    _, succeeded, failed, stopped = message
                    self.set_busy(False)
                    self.status.set(f"本批次完成：成功 {succeeded}，失败 {failed}。" + ("后续任务已暂停，可点击开始继续。" if stopped else "选择记录可查看详情。"))
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def cancel_pending(self):
        self.stop_after_current.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("当前文件会继续安全完成，之后不再启动下一个文件。")

    def open_output(self):
        selected = self.tree.selection()
        target = self.entries[selected[0]]["output"] if selected else None
        target = target if target and target.is_dir() else Path(self.output.get())
        if target.is_dir():
            os.startfile(str(target))
        else:
            messagebox.showinfo("目录尚未生成", "请先选择有效目录或完成一次导出。")

    def close(self):
        if self.active:
            messagebox.showinfo("正在导出", "请先取消后续任务，等待当前文件完成后关闭，避免留下不完整视频。")
            return
        self.root.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="事件与灰度视频手动导出助手")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--types", nargs="+", choices=("joint", "events", "gray"), default=["joint", "events", "gray"])
    args = parser.parse_args()
    if args.worker:
        if args.output is None:
            parser.error("worker模式需要--output")
        sys.exit(worker(args.worker, args.output, args.fps, args.types))
    window = tk.Tk()
    app = ExportAssistant(window)
    window.mainloop()
