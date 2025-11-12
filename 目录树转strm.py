import os
import re
import json
import urllib.parse
import traceback
import time
import threading
import shutil
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
from tkinterdnd2 import TkinterDnD, DND_FILES
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- 全局常量 ---

# 定位脚本的真实目录，确保配置文件路径始终正确
try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
except NameError:
    # 备用方案 (例如在某些打包环境中 __file__ 未定义)
    script_dir = os.getcwd()

# 配置文件 (保存UI设置)
CONFIG_FILE = os.path.join(script_dir, 'config.json')

# 媒体文件扩展名过滤
VIDEO_EXTS = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.ts', '.rmvb']

def trim_path_by_keyword(path, keyword):
    """
    以 keyword 为开始标志，截取 path 中 keyword 及其之后的部分。
    返回以 / 开头的标准化相对路径。
    """
    if not keyword:
        p = path.replace('\\', '/')
        p = '/' + p.lstrip('/')
        while p.startswith('//'):
            p = p[1:]
        return p

    keyword = keyword.replace('\\', '/')
    path = path.replace('\\', '/')

    pos = path.find(keyword)
    if pos == -1:
        p = '/' + path.lstrip('/')
        while p.startswith('//'):
            p = p[1:]
        return p

    sub = path[pos:]
    if not sub.startswith('/'):
        sub = '/' + sub
    while sub.startswith('//'):
        sub = sub[1:]
    return sub

class StrmGeneratorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("115 目录树转 STRM 工具 (优化版)")
        self.root.geometry("960x720") 
        
        # 缓存解析结果
        self.all_media_paths = []
        self.folder_choices = set()
        
        self.selected_folders = set()
        self.last_mode = None  # 保存最后选择的模式
        
        # 线程锁，防止加载/生成时冲突
        self._is_loading = threading.Lock()
        
        self.create_widgets()
        self.load_config()

    def create_widgets(self):
        # --- 1. 配置框架 (Top) ---
        frame = tk.LabelFrame(self.root, text="🚀 基本配置", padx=10, pady=10)
        frame.pack(side='top', padx=10, pady=10, fill='x')

        tk.Label(frame, text="① 目录树文件路径：").grid(row=0, column=0, sticky='w', pady=5)
        self.path_var = tk.StringVar()
        path_entry = tk.Entry(frame, textvariable=self.path_var, width=70)
        path_entry.grid(row=0, column=1, padx=5, sticky='ew')
        path_entry.drop_target_register(DND_FILES)
        path_entry.dnd_bind('<<Drop>>', self.on_drop_files)
        tk.Button(frame, text="浏览", command=self.browse_file).grid(row=0, column=2, padx=5)

        tk.Label(frame, text="② openlist 链接前缀：").grid(row=1, column=0, sticky='w', pady=5)
        self.prefix_var = tk.StringVar()
        tk.Entry(frame, textvariable=self.prefix_var, width=70).grid(row=1, column=1, columnspan=2, padx=5, sticky='ew')

        tk.Label(frame, text="③ STRM 输出目录：").grid(row=2, column=0, sticky='w', pady=5)
        self.output_var = tk.StringVar()
        tk.Entry(frame, textvariable=self.output_var, width=70).grid(row=2, column=1, padx=5, sticky='ew')
        tk.Button(frame, text="浏览", command=self.browse_output).grid(row=2, column=2, padx=5)
        
        tk.Label(frame, text="④ 开始标志关键词 (留空即从头开始)：").grid(row=3, column=0, sticky='w', pady=5)
        self.start_keyword_var = tk.StringVar()
        tk.Entry(frame, textvariable=self.start_keyword_var, width=30).grid(row=3, column=1, sticky='w', padx=5)

        tk.Label(frame, text="⑤ 输出文件扩展名：").grid(row=4, column=0, sticky='w', pady=5)
        self.ext_var = tk.StringVar(value=".strm")
        tk.Entry(frame, textvariable=self.ext_var, width=10).grid(row=4, column=1, sticky='w', padx=5)

        self.encode_var = tk.BooleanVar(value=True)
        tk.Checkbutton(frame, text="链接自动 URL 编码", variable=self.encode_var).grid(row=4, column=1, sticky='e', padx=(0, 150)) 
        
        self.save_var = tk.BooleanVar(value=True)
        tk.Checkbutton(frame, text="保存设置", variable=self.save_var).grid(row=4, column=2, sticky='w')

        # 确保路径输入框可以拉伸
        frame.grid_columnconfigure(1, weight=1)

        # --- 2. 操作按钮 (Middle) ---
        btn_frame = tk.LabelFrame(self.root, text="🔨 操作模式", padx=10, pady=6)
        btn_frame.pack(side='top', pady=6, fill='x', padx=10)

        # 按钮平铺
        tk.Button(btn_frame, text="📂 载入目录树 (第一步)", width=20, command=self.load_tree_only).pack(side='left', padx=6, expand=True)
        tk.Button(btn_frame, text="🔥 全量生成", width=20, fg='red', command=self.confirm_and_start_full_generation).pack(side='left', padx=6, expand=True)
        tk.Button(btn_frame, text="🔄 增量生成", width=20, command=lambda: self.start_generation(mode='increment')).pack(side='left', padx=6, expand=True)
        tk.Button(btn_frame, text="✅ 选择目录生成", width=20, command=self.show_folder_selector).pack(side='left', padx=6, expand=True)

        # --- 3. 布局 (Top/Bottom/Fill) ---
        
        # 状态栏 (Bottom)
        self.status_var = tk.StringVar(value="✅ 等待开始...")
        tk.Label(self.root, textvariable=self.status_var, anchor='w', fg='blue', font=('Arial', 10, 'bold')).pack(side='bottom', fill='x', padx=10, pady=5)

        # 日志输出 (Fill)
        tk.Label(self.root, text="📜 日志输出：").pack(side='top', anchor='w', padx=10)
        
        self.log_text = scrolledtext.ScrolledText(self.root, width=120, height=28)
        self.log_text.pack(side='top', padx=10, pady=5, fill='both', expand=True) 
        
        self.log_text.config(state='disabled') # 设为只读
        
        # 日志区域也支持拖放
        self.log_text.drop_target_register(DND_FILES)
        self.log_text.dnd_bind('<<Drop>>', self.on_drop_files)

    # --- UI 辅助方法 ---
    def browse_file(self):
        path = filedialog.askopenfilename(filetypes=[("文本文件", "*.txt")])
        if path:
            self.path_var.set(path)
            self.save_config()
            self.load_tree_only() # 异步加载

    def browse_output(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_var.set(folder)
            self.save_config()

    def on_drop_files(self, event):
        # 清理 Windows 拖放时可能带入的花括号
        try:
            files_raw = self.root.tk.splitlist(event.data)
            files = [f.strip('{}') for f in files_raw]
        except Exception:
            files = [event.data] # 备用方案

        valid_txt_files = [f for f in files if f.lower().endswith('.txt')]
        
        if valid_txt_files:
            dropped_file = valid_txt_files[0] # 只取第一个
            self.path_var.set(dropped_file)
            self.log(f"[拖入] 已设置目录树文件: {dropped_file}")
            self.save_config() 
            self.load_tree_only() # 异步加载
        else:
            self.log("[拖入] 拖入的文件不是 .txt 文件。")


    def log(self, text):
        """线程安全的日志记录 (智能滚动, 只读)"""
        if threading.current_thread() is threading.main_thread():
            is_at_bottom = True 
            try:
                # 检查滚动条是否在底部
                scroll_y = self.log_text.yview() 
                is_at_bottom = scroll_y[1] > 0.99
            except tk.TclError:
                # 窗口尚未完全初始化时，yview() 可能会失败
                pass
            
            # 临时启用控件以写入日志
            self.log_text.config(state='normal')
            
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self.log_text.insert(tk.END, f"[{ts}] {text}\n")
            
            if is_at_bottom:
                self.log_text.see(tk.END) # 仅在已到底部时才自动滚动
            
            self.log_text.config(state='disabled')
        else:
            # 在工作线程中，调度回主线程
            self.root.after(0, self.log, text)

    # --- 配置加载与保存 ---
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                self.path_var.set(config.get('path', ''))
                self.prefix_var.set(config.get('prefix', ''))
                self.output_var.set(config.get('output', ''))
                self.ext_var.set(config.get('ext', '.strm'))
                self.start_keyword_var.set(config.get('start_keyword', ''))
                self.save_var.set(config.get('save_config', True))
                self.log(f"[配置] 成功加载配置文件: {CONFIG_FILE}")
            except Exception as e:
                self.log(f"[错误] 配置文件 {CONFIG_FILE} 读取失败: {e}")
        else:
            self.log("[配置] 未找到配置文件，使用默认设置。")


    def save_config(self, mode=None):
        if self.save_var.get():
            config = {
                'path': self.path_var.get(),
                'prefix': self.prefix_var.get(),
                'output': self.output_var.get(),
                'ext': self.ext_var.get(),
                'start_keyword': self.start_keyword_var.get(),
                'last_mode': mode or self.last_mode,
                'save_config': self.save_var.get()
            }
            try:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.log(f"[错误] 保存配置 {CONFIG_FILE} 失败: {e}")
        elif os.path.exists(CONFIG_FILE):
            # 如果取消勾选“保存设置”，则删除配置文件
            try:
                os.remove(CONFIG_FILE)
            except Exception as e:
                self.log(f"[警告] 删除配置文件 {CONFIG_FILE} 失败: {e}")

    def _backup_index_file(self, index_file_path):
        """
        创建索引文件的备份 (.bak)，并保存在脚本 (script_dir) 目录下。
        这为 "全量生成" 或 "增量生成" 提供了单次容错机会。
        """
        if not os.path.exists(index_file_path):
            return  # 没有可备份的文件

        try:
            # 备份到脚本目录，而不是输出目录
            index_filename = os.path.basename(index_file_path)
            backup_filename = index_filename + ".bak"
            backup_file_path = os.path.join(script_dir, backup_filename) 

            # copy2 会覆盖旧的备份
            shutil.copy2(index_file_path, backup_file_path)
            self.log(f"[索引] 已备份当前索引文件到: {backup_file_path}")
        except Exception as e:
            self.log(f"[错误] 备份索引文件失败: {e}")

    # --- 核心逻辑：目录树解析 ---
    def read_text_file_with_fallback(self, path):
        # 尝试多种可能的编码
        for enc in ['utf-8', 'utf-16', 'utf-8-sig', 'gb18030', 'gbk']:
            try:
                with open(path, 'r', encoding=enc) as f:
                    return f.readlines()
            except UnicodeDecodeError:
                continue
        raise UnicodeDecodeError("read", b"", 0, 1, "文件编码错误，建议另存为 UTF-8")

    def parse_directory_tree(self, lines):
        """
        解析目录树文本，返回媒体文件的逻辑路径列表。
        使用 VIDEO_EXTS 进行过滤。
        """
        paths = []
        stack = []
        processing = False
        start_keyword = self.start_keyword_var.get().strip()

        if not start_keyword:
            processing = True

        for line in lines:
            line = line.rstrip('\n\r')
            if not line.strip():
                continue
            
            # 如果设置了关键词，则跳过直到找到它
            if start_keyword and not processing:
                if start_keyword in line:
                    stack = [] # 找到关键词，重置堆栈并开始处理
                    processing = True
                continue 
            
            if not processing:
                continue

            match = re.match(r'^([| ]+)[|\\/\-]+(.*)', line)
            if match:
                prefix = match.group(1)
                name = match.group(2).strip()
                # 使用 | 的数量来估计深度，忽略空格
                depth = len(prefix.replace(' ', ''))
                
                # 栈操作，保持目录路径
                while len(stack) > depth:
                    stack.pop()

                # 兼容可能的跳级目录（例如深度0直接到深度2）
                while len(stack) < depth:
                    stack.append("") 

                if len(stack) == depth:
                    # 替换当前深度的最后一项
                    if stack:
                        stack[-1] = name
                    else: # 针对 depth=0 的情况
                        stack.append(name)
                elif len(stack) == depth - 1:
                     # 正常进入下一级
                    stack.append(name)
                else:
                    # 树结构跳跃或解析错误，重置堆栈到当前深度
                    stack = stack[:depth] + [name]

                full_path = '/'.join(stack)
                # 只保留媒体文件
                if any(name.lower().endswith(ext) for ext in VIDEO_EXTS):
                    paths.append(full_path)
        return paths

    # --- 核心逻辑：异步加载 ---
    def _load_tree_blocking(self):
        """
        实际的 I/O 和解析 (在工作线程中运行)。
        成功时返回 (all_media_paths, folder_set)，失败时返回 None。
        """
        input_path = self.path_var.get()
        if not input_path or not os.path.exists(input_path):
            self.log("[错误] 载入失败：目录树文件路径无效。")
            self.root.after(0, lambda: self.status_var.set("❌ 目录树文件路径无效！"))
            return None
            
        try:
            lines = self.read_text_file_with_fallback(input_path) 
            all_media_paths = self.parse_directory_tree(lines)
            folder_set = sorted(set(os.path.dirname(p) for p in all_media_paths))
            return (all_media_paths, folder_set)
        except Exception as e:
            self.log(f"[错误] 解析目录树失败: {e}")
            self.log(traceback.format_exc())
            self.root.after(0, lambda: self.status_var.set("❌ 解析失败！请检查文件编码或格式。"))
            return None


    def load_tree_only(self, callback=None):
        """
        异步加载目录树 (UI 线程调用)。
        使用锁 _is_loading 防止并发操作。
        """
        if not self._is_loading.acquire(blocking=False):
            self.log("[提示] 正在处理中，请稍候...")
            if callback:
                self.root.after(0, lambda: callback(False))
            return

        self.root.after(0, lambda: self.status_var.set("🔄 正在载入目录树..."))
        
        def worker():
            try:
                results = self._load_tree_blocking()
                
                if results is None:
                    # 捕获加载失败
                    if callback:
                        self.root.after(0, lambda: callback(False))
                    return
                
                all_media_paths, folder_set = results

                # 成功 - 调度UI更新
                def update_ui_with_load_results():
                    self.all_media_paths = all_media_paths
                    self.folder_choices = set(folder_set)
                    self.selected_folders = set() # 清空上次的选择
                    
                    self.log(f"[载入] 成功解析 {len(self.all_media_paths)} 个媒体文件，{len(folder_set)} 个文件夹。")
                    self.status_var.set(f"✅ 目录树载入完成，共 {len(self.all_media_paths)} 个文件。")
                    if callback:
                        callback(True)

                self.root.after(0, update_ui_with_load_results)

            except Exception as e:
                # 捕获意外错误
                def log_load_error():
                    self.log(f"[错误] 载入目录树时发生意外: {e}")
                    self.log(traceback.format_exc())
                    self.status_var.set("❌ 载入失败！请检查日志。")
                    if callback:
                        callback(False)
                self.root.after(0, log_load_error)
            finally:
                # 确保锁总是被释放
                self._is_loading.release()
        
        t = threading.Thread(target=worker, daemon=True)
        t.start()


    # --- 核心逻辑：文件夹选择窗口 ---
    def show_folder_selector(self):
        # 1. 检查缓存，如果为空则异步加载
        if not self.folder_choices:
            self.log("[提示] 文件夹列表为空，正在尝试自动载入...")
            
            def on_load_complete(success):
                if success and self.folder_choices:
                    self.show_folder_selector() # 重新调用
                elif not success:
                    self.log("[错误] 自动载入失败，无法打开目录选择器。")
                else:
                    self.log("[错误] 无法载入文件夹列表。请检查目录树文件。")
            
            self.load_tree_only(callback=on_load_complete)
            return

        # 2. 创建窗口
        win = tk.Toplevel(self.root)
        win.title("选择要生成的目录")
        win.geometry("750x550")
        
        # --- 窗口布局 (三明治布局) ---

        # 1. 底部按钮 (锚定)
        bottom_frame = tk.Frame(win)
        bottom_frame.pack(side='bottom', fill='x', pady=(5, 10))
        
        inner_buttons_frame = tk.Frame(bottom_frame)
        inner_buttons_frame.pack() # 居中
        
        btn_confirm = tk.Button(inner_buttons_frame, text="确认生成", width=12)
        btn_confirm.pack(side='left', padx=10)
        btn_cancel = tk.Button(inner_buttons_frame, text="取消", width=12, command=win.destroy)
        btn_cancel.pack(side='left', padx=10)

        # 2. 顶部搜索框
        filter_frame = tk.LabelFrame(win, text="🔍 筛选目录", padx=10, pady=5)
        filter_frame.pack(side='top', fill='x', padx=10, pady=(10, 5)) 

        search_var = tk.StringVar()
        search_entry = tk.Entry(filter_frame, textvariable=search_var, width=50)
        search_entry.pack(side='left', fill='x', expand=True, padx=5)

        # 3. 中间列表 (填充剩余空间)
        list_frame = tk.LabelFrame(win, text="📂 目录列表 (可多选)", padx=10, pady=10)
        list_frame.pack(side='top', fill='both', expand=True, padx=10, pady=5)

        # 列表区的快速选择按钮
        select_btn_frame = tk.Frame(list_frame)
        select_btn_frame.pack(fill='x', pady=(0, 5))
        tk.Button(select_btn_frame, text="全选", width=10, command=lambda: select_all(True)).pack(side='left', padx=5)
        tk.Button(select_btn_frame, text="全不选", width=10, command=lambda: select_all(False)).pack(side='left', padx=5)

        # 列表
        scrollbar = tk.Scrollbar(list_frame, orient='vertical')
        listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, selectmode='extended', height=20)
        scrollbar.config(command=listbox.yview)

        scrollbar.pack(side='right', fill='y')
        listbox.pack(side='left', fill='both', expand=True)

        # --- 窗口功能 ---
        
        sorted_folders = sorted(list(self.folder_choices))

        def populate_listbox(items):
            """填充列表，并选中已选的项目"""
            listbox.delete(0, tk.END)
            for i, folder in enumerate(items):
                listbox.insert(tk.END, folder)
                if folder in self.selected_folders:
                    listbox.select_set(i)

        def on_filter(*args):
            """根据搜索框内容筛选列表"""
            query = search_var.get().lower()
            if not query:
                populate_listbox(sorted_folders)
            else:
                filtered_items = [f for f in sorted_folders if query in f.lower()]
                populate_listbox(filtered_items)
        
        search_var.trace_add('write', on_filter) # 绑定搜索事件
        
        def select_all(select_all_flag):
            """全选或全不选"""
            if select_all_flag:
                listbox.select_set(0, tk.END)
            else:
                listbox.select_clear(0, tk.END)

        def on_confirm():
            selected_indices = listbox.curselection()
            if not selected_indices:
                self.log("[提示] 你没有选择任何目录。")
                win.destroy()
                return
            
            self.selected_folders = {listbox.get(i) for i in selected_indices}
            
            self.log(f"[选择] 已选择 {len(self.selected_folders)} 个目录准备生成。")
            win.destroy()
            
            self.start_generation(mode='single') 
        
        btn_confirm.config(command=on_confirm)
        win.protocol("WM_DELETE_WINDOW", win.destroy) # 绑定窗口关闭事件
        populate_listbox(sorted_folders)

        win.transient(self.root)
        win.grab_set()
        self.root.wait_window(win)

    # --- 核心逻辑：全量生成确认 ---
    def confirm_and_start_full_generation(self):
        """点击全量生成按钮后调用，负责确认"""
        
        # 检查缓存，如果为空则异步加载
        if not self.all_media_paths:
            self.log("[提示] 目录树未载入，正在尝试自动载入...")
            
            def on_load_complete(success):
                if success and self.all_media_paths:
                    self.confirm_and_start_full_generation() 
                elif not success:
                    self.log("[错误] 自动载入失败，无法全量生成。")
                else:
                     self.log("[错误] 目录树为空，请先载入文件。")
                     self.root.after(0, lambda: self.status_var.set("❌ 目录树为空"))

            self.load_tree_only(callback=on_load_complete)
            return

        # 显示确认对话框
        message = (f"您确定要执行 **全量生成** 吗？\n\n"
                   f"此操作将根据当前目录树文件 (共 {len(self.all_media_paths)} 个媒体文件) "
                   f"在输出目录中重新生成所有 STRM 文件。\n"
                   f"⚠️ 【警告】这会覆盖输出目录下已存在的同名 STRM 文件！")
        
        if messagebox.askyesno("全量生成确认", message):
            self.log("[确认] 用户已确认全量生成。")
            self.start_generation(mode='full')
        else:
            self.log("[取消] 用户取消了全量生成操作。")
            self.root.after(0, lambda: self.status_var.set("✅ 全量生成已取消。"))


    # --- 核心逻辑：生成 STRM (工作线程) ---
    def start_generation(self, mode='full'):
        self.last_mode = mode
        t = threading.Thread(target=self._worker_generate, args=(mode,))
        t.daemon = True
        t.start()

    def _worker_generate(self, mode):
        # 使用锁防止并发生成
        if not self._is_loading.acquire(blocking=False):
            self.log("[错误] 无法开始生成：当前正在载入目录树。请稍后再试。")
            self.root.after(0, lambda: self.status_var.set("❌ 操作冲突，请等待载入完成"))
            return

        try:
            self.root.after(0, lambda: self.status_var.set("🔄 处理中..."))
            self.log(f"开始 {mode} 模式生成 STRM 文件...")
            input_path = self.path_var.get()
            prefix = self.prefix_var.get().rstrip('/')
            output_dir = self.output_var.get()
            ext = self.ext_var.get()
            start_keyword = self.start_keyword_var.get().strip()
            encode_url = self.encode_var.get()

            # --- 路径和前缀检查 ---
            if not input_path or not os.path.exists(input_path):
                self.log("[错误] 目录树文件路径无效！")
                self.root.after(0, lambda: self.status_var.set("❌ 目录树文件路径无效！"))
                return # finally 会释放锁
            if not prefix:
                self.log("[错误] 请填写 openlist 链接前缀！")
                self.root.after(0, lambda: self.status_var.set("❌ 链接前缀为空！"))
                return
            if not output_dir:
                self.log("[错误] STRM 输出目录为空！")
                self.root.after(0, lambda: self.status_var.set("❌ STRM 输出目录为空！"))
                return

            # --- 检查缓存，如果为空则阻塞等待加载 ---
            if not self.all_media_paths:
                self.log("[提示] 缓存为空，正在自动载入目录树...")
                load_event = threading.Event()
                
                def load_wrapper():
                    # 在主线程中执行
                    def on_load_complete(success):
                        if not success:
                            self.log("[错误] _worker_generate 自动载入失败。")
                        load_event.set() # 通知工作线程继续
                    
                    # 释放 _worker_generate 的锁，让 load_tree_only 能获取
                    self._is_loading.release()
                    self.load_tree_only(callback=on_load_complete)

                self.root.after(0, load_wrapper)
                load_event.wait() # 工作线程在此等待
                
                # 重新获取锁
                self._is_loading.acquire() 

                if not self.all_media_paths:
                    self.log("[错误] 载入目录树失败或目录树为空。")
                    self.root.after(0, lambda: self.status_var.set("❌ 目录树为空"))
                    return

            # --- 文件夹选择逻辑 ---
            if mode == 'full' or mode == 'increment':
                self.selected_folders = self.folder_choices
                self.log(f"[模式] {mode} 模式：将处理全部 {len(self.folder_choices)} 个文件夹。")

            media_paths = [p for p in self.all_media_paths if os.path.dirname(p) in self.selected_folders]
            
            if not media_paths:
                self.log("[提示] 没有在选定文件夹中找到符合条件的媒体文件。")
                self.root.after(0, lambda: self.status_var.set("⚠️ 没有符合条件的文件。"))
                return
                
            self.log(f"[过滤] 共有 {len(media_paths)} 个文件待处理...")

            
            files_to_generate = [] 
            index_file = os.path.join(output_dir, '.strm_index.json')
            new_index = {}
            old_index = {}
            added = []
            removed = []

            if mode == "increment":
                if os.path.exists(index_file):
                    try:
                        with open(index_file, 'r', encoding='utf-8') as f:
                            old_index = json.load(f)
                    except Exception:
                        old_index = {}

                for path in media_paths:
                    fp = trim_path_by_keyword(path, start_keyword)
                    new_index[fp] = True
                    if fp not in old_index:
                        added.append(path) # added 列表使用原始路径
                removed = [p for p in old_index.keys() if p not in new_index]

                self.log(f"[对比] 新增: {len(added)} , 删除: {len(removed)}")

                if added or removed:
                    files_to_generate = self.preview_selection(added, removed)
                else:
                    self.log("[提示] 没有新增或删除项目，增量生成结束。")
                    self.root.after(0, lambda: self.status_var.set("✅ 增量生成完成，无需操作"))
                    files_to_generate = [] 
                
                # 只有在用户确认生成后才保存新索引
                if files_to_generate:
                    try:
                        self._backup_index_file(index_file) # 备份旧索引
                        with open(index_file, 'w', encoding='utf-8') as f:
                            json.dump(new_index, f, ensure_ascii=False, indent=2)
                        self.log(f"[索引] 增量模式：已保存新索引 (共 {len(new_index)} 项)。")
                    except Exception as e:
                            self.log(f"[错误] 保存 'increment' 模式索引失败: {e}")

            elif mode == "single":
                self.log("[模式] 在选定目录中进行文件级选择...")
                files_to_generate = self.preview_selection(media_paths, [])
            
            elif mode == "full":
                self.log("[模式] 全量生成，跳过预览，直接处理所有文件...")
                files_to_generate = media_paths 
            
            # --- 写入 STRM 文件 ---
            count = 0
            if not files_to_generate:
                self.log("[提示] 没有需要写入的文件。")
                self.root.after(0, lambda: self.status_var.set("✅ 完成，无需写入。"))
                return
                
            successful_writes_index = {}
            index_lock = threading.Lock()
             
            cpu_count = os.cpu_count() or 4
            max_workers = min(64, max(4, cpu_count * 4))
            self.log(f"[多线程] 启用 {max_workers} 个并发线程进行 STRM 写入...")

            def write_strm(mp):
                try:
                    base = os.path.basename(mp)
                    name_without_ext = os.path.splitext(base)[0]
                    # 清理文件名中的非法字符
                    safe_name = re.sub(r'[\\/:*?"<>|]', '_', name_without_ext)
                    safe_name = safe_name.strip()
                    if not safe_name:
                        safe_name = f"__invalid_name_{int(time.time()*1000)}"

                    file_name = safe_name + (ext if ext.startswith('.') else '.' + ext)
                    trimmed_path = trim_path_by_keyword(mp, start_keyword)
                    relative_dir = os.path.dirname(trimmed_path).lstrip('/\\')
                    target_dir = os.path.join(output_dir, relative_dir) if relative_dir else output_dir
                    os.makedirs(target_dir, exist_ok=True)
                    
                    # URL 编码：只编码路径的每一部分，不编码 /
                    trimmed_path_nix = trimmed_path.replace('\\', '/')
                    url_path_parts = (urllib.parse.quote(p) for p in trimmed_path_nix.split('/'))
                    url_path = '/'.join(url_path_parts) if encode_url else trimmed_path_nix
                    
                    full_url = f"{prefix}/{url_path.lstrip('/')}"
                    # 修正协议和路径之间的多余斜杠
                    full_url = re.sub(r'(?<!:)/{2,}', '/', full_url) 
                    
                    output_path = os.path.join(target_dir, file_name)
                    
                    with open(output_path, 'w', encoding='utf-8') as f:
                        f.write(full_url + '\n')
                        
                    return f"[写入] {output_path} → {full_url}", 1, trimmed_path
                except Exception as e:
                    return f"[失败] 写入 {mp} 错误: {e}", 0, None

            if files_to_generate:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(write_strm, p): p for p in files_to_generate}
                    for future in as_completed(futures):
                        src = futures.get(future)
                        try:
                            result, ret, trimmed_path_or_none = future.result() 
                            
                            if ret == 1:
                                count += 1
                                if (mode == 'full' or mode == 'single') and trimmed_path_or_none:
                                    with index_lock:
                                        successful_writes_index[trimmed_path_or_none] = True
                            else:
                                self.log(result) 
                        except Exception as e:
                            self.log(f"[线程异常] 处理 {src} 时报错: {e}")

            # --- 最终索引保存 ---
            if mode == "full":
                try:
                    self._backup_index_file(index_file) # 备份旧索引
                    with open(index_file, 'w', encoding='utf-8') as f:
                        json.dump(successful_writes_index, f, ensure_ascii=False, indent=2)
                    self.log(f"[索引] 全量模式：已为 {len(successful_writes_index)} 个【成功写入】的文件保存索引。")
                except Exception as e:
                        self.log(f"[错误] 保存 'full' 模式索引失败: {e}")

            elif mode == "single":
                if successful_writes_index:
                    old_index = {}
                    if os.path.exists(index_file):
                        try:
                            with open(index_file, 'r', encoding='utf-8') as f:
                                old_index = json.load(f)
                        except Exception:
                            pass
                    
                    # 合并：保留旧索引，并添加新生成的项
                    final_index = old_index.copy()
                    final_index.update(successful_writes_index)
                    
                    try:
                        self._backup_index_file(index_file) # 备份旧索引
                        with open(index_file, 'w', encoding='utf-8') as f:
                            json.dump(final_index, f, ensure_ascii=False, indent=2)
                        self.log(f"[索引] 选择了目录模式，已【增量更新】全局索引 (新增 {len(successful_writes_index)} 项)。")
                    except Exception as e:
                           self.log(f"[错误] 保存 'single' 模式索引失败: {e}")
                else:
                    self.log("[提示] '选择目录生成' (single 模式) 完成。未写入文件，索引未更新。")
            # --------------------

            self.log(f"[完成] 共生成 {count} 个 STRM 文件。")
            self.root.after(0, lambda: self.status_var.set(f"✅ 完成，生成 {count} 个文件。"))
            self.save_config(mode)

        except Exception as e:
            self.log(f"[异常] 生成过程中出现错误: {e}")
            self.log(traceback.format_exc())
            self.root.after(0, lambda: self.status_var.set("❌ 生成失败！"))
        finally:
            # 确保锁在任何情况下都被释放
            if self._is_loading.locked():
                 self._is_loading.release()

    # --- 核心逻辑：增量预览窗口 ---
    def preview_selection(self, added, removed):
        """
        在工作线程中被调用，但会调度一个 Toplevel 窗口到主线程。
        使用 Event 来阻塞工作线程，直到窗口关闭。
        """
        confirm_event = threading.Event()
        preview_result = {'to_generate': []}

        def show_preview():
            win = tk.Toplevel(self.root)
            win.title("选择生成项")
            win.geometry("820x520")

            tk.Label(win, text=f"新增: {len(added)}  删除: {len(removed)}").pack(anchor='w', padx=10, pady=6)
            
            # --- 窗口布局 (三明治布局) ---
            
            # 1. 底部按钮 (锚定)
            btn_frame = tk.Frame(win)
            btn_frame.pack(side='bottom', pady=6) 

            # 1b. 按钮居中
            inner_buttons_frame = tk.Frame(btn_frame)
            inner_buttons_frame.pack() 

            # 2. 删除区域 (锚定在按钮上方)
            if removed:
                frame_del = tk.LabelFrame(win, text="已移除项（仅参考，这些 STRM 文件可能需要手动删除）", padx=6, pady=6)
                frame_del.pack(side='bottom', fill='x', expand=False, padx=10, pady=6) 
                del_txt = scrolledtext.ScrolledText(frame_del, width=96, height=8)
                del_txt.pack(fill='both', expand=True)
                for r in removed:
                    del_txt.insert(tk.END, f"{r}\n")
                del_txt.configure(state='disabled')

            # 3. 新增区域 (填充剩余空间)
            frame_add = tk.LabelFrame(win, text="可选生成项 (勾选的项目将被生成)", padx=6, pady=6)
            frame_add.pack(fill='both', expand=True, padx=10, pady=6) 
            
            # --- Canvas 列表 ---
            canvas_a = tk.Canvas(frame_add)
            scrollbar_a = tk.Scrollbar(frame_add, orient="vertical", command=canvas_a.yview)
            inner_a = tk.Frame(canvas_a)
            
            def configure_scroll(event):
                canvas_a.configure(scrollregion=canvas_a.bbox("all"))
            inner_a.bind("<Configure>", configure_scroll)
            
            canvas_a.create_window((0,0), window=inner_a, anchor='nw')
            canvas_a.configure(yscrollcommand=scrollbar_a.set)
            
            scrollbar_a.pack(side="right", fill="y")
            canvas_a.pack(side="left", fill='both', expand=True)

            # --- 窗口功能 ---
            added_vars_map = {} 
            
            MAX_PREVIEW_ITEMS = 5000
            if len(added) > MAX_PREVIEW_ITEMS:
                tk.Label(inner_a, text=f"项目过多 ({len(added)} 个)，超过 {MAX_PREVIEW_ITEMS} 条预览限制。\n将默认全部生成。", fg='red').pack(pady=20)
                preview_result['to_generate'] = list(added)
            else:
                # 分块加载 Checkbutton 以防止UI冻结
                def load_chunk(index=0):
                    try:
                        CHUNK_SIZE = 200 # 每次加载 200 个
                        count = 0
                        while count < CHUNK_SIZE and index < len(added):
                            p = added[index]
                            var = tk.BooleanVar(value=True)
                            cb = tk.Checkbutton(inner_a, text=p, variable=var, anchor='w')
                            cb.pack(anchor='w', fill='x')
                            added_vars_map[p] = var
                            count += 1
                            index += 1
                        
                        if index < len(added):
                            win.after(1, lambda: load_chunk(index))
                    except tk.TclError:
                        # 窗口在加载过程中被关闭
                        pass
                
                load_chunk(0) # 开始加载

            # 按钮回调
            def on_confirm():
                if len(added) <= MAX_PREVIEW_ITEMS:
                    preview_result['to_generate'] = [p for p,var in added_vars_map.items() if var.get()]
                win.destroy()
                confirm_event.set()
                
            def on_cancel():
                preview_result['to_generate'] = []
                win.destroy()
                confirm_event.set()
                
            # 添加按钮到居中框架
            tk.Button(inner_buttons_frame, text="确认生成所选项", command=on_confirm).pack(side='left', padx=8)
            tk.Button(inner_buttons_frame, text="取消（不生成）", command=on_cancel).pack(side='left', padx=8)

            win.protocol("WM_DELETE_WINDOW", on_cancel) # 绑定窗口关闭事件
            win.transient(self.root)
            win.grab_set()
            self.root.wait_window(win)
            
            if not confirm_event.is_set():
                 preview_result['to_generate'] = []
                 confirm_event.set()

        self.root.after(0, show_preview)
        confirm_event.wait() 
        return preview_result.get('to_generate', [])

def main():
    # 确保使用 TkinterDnD.Tk() 来支持文件拖放
    root = TkinterDnD.Tk()
    app = StrmGeneratorApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
