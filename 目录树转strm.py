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

# 基础设置
try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
except NameError:
    script_dir = os.getcwd()

CONFIG_FILE = os.path.join(script_dir, 'config.json')

# 视频格式过滤
VIDEO_EXTS = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.ts', '.rmvb', '.iso', '.wmv']

# 预编译正则
TREE_LINE_PATTERN = re.compile(r'^([| ]+)[|\\/\-]+(.*)')

def trim_path_by_keyword(path, keyword):
    """
    正则太慢，改用字符串 find 截取，几万个文件也能秒解。
    """
    p = path.replace('\\', '/')
    
    if not keyword:
        return '/' + p.lstrip('/')

    # 转小写定位
    idx = p.lower().find(keyword.replace('\\', '/').lower())

    if idx != -1:
        sub = p[idx:]
        if not sub.startswith('/'):
            sub = '/' + sub
        while '//' in sub:
            sub = sub.replace('//', '/')
        return sub
    else:
        return '/' + p.lstrip('/')

class StrmGeneratorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("115 目录树转 STRM 工具 (极速版)")
        self.root.geometry("960x720") 
        
        self.all_media_paths = []
        self.folder_choices = set()
        self.selected_folders = set()
        self.last_mode = None 
        
        self._is_loading = threading.Lock()
        
        self.create_widgets()
        self.load_config()

    def create_widgets(self):
        # 1. 配置区
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
        tk.Checkbutton(frame, text="链接自动 URL 编码", variable=self.encode_var).grid(row=4, column=1, padx=(120, 0), sticky='w') 
        
        # 自动载入开关
        self.auto_load_latest_var = tk.BooleanVar(value=True)
        tk.Checkbutton(frame, text="自动载入同目录最新文件", variable=self.auto_load_latest_var, fg='blue').grid(row=4, column=1, sticky='e', padx=(0, 10))

        self.save_var = tk.BooleanVar(value=True)
        tk.Checkbutton(frame, text="保存设置", variable=self.save_var).grid(row=4, column=2, sticky='w')

        frame.grid_columnconfigure(1, weight=1)

        # 2. 按钮区
        btn_frame = tk.LabelFrame(self.root, text="🔨 操作模式", padx=10, pady=6)
        btn_frame.pack(side='top', pady=6, fill='x', padx=10)

        tk.Button(btn_frame, text="📂 载入目录树 (第一步)", width=20, command=self.load_tree_only).pack(side='left', padx=6, expand=True)
        tk.Button(btn_frame, text="🔥 全量生成", width=20, fg='red', command=self.confirm_and_start_full_generation).pack(side='left', padx=6, expand=True)
        tk.Button(btn_frame, text="🔄 增量生成", width=20, command=lambda: self.start_generation(mode='increment')).pack(side='left', padx=6, expand=True)
        tk.Button(btn_frame, text="✅ 选择目录生成", width=20, command=self.show_folder_selector).pack(side='left', padx=6, expand=True)

        # 3. 日志区
        self.status_var = tk.StringVar(value="✅ 等待开始...")
        tk.Label(self.root, textvariable=self.status_var, anchor='w', fg='blue', font=('Arial', 10, 'bold')).pack(side='bottom', fill='x', padx=10, pady=5)

        tk.Label(self.root, text="📜 日志输出：").pack(side='top', anchor='w', padx=10)
        self.log_text = scrolledtext.ScrolledText(self.root, width=120, height=28)
        self.log_text.pack(side='top', padx=10, pady=5, fill='both', expand=True) 
        self.log_text.config(state='disabled')
        
        self.log_text.drop_target_register(DND_FILES)
        self.log_text.dnd_bind('<<Drop>>', self.on_drop_files)

    # 找同目录下最新的文件
    def _find_latest_file(self, current_file_path):
        if not current_file_path: return None
        directory = os.path.dirname(current_file_path)
        if not os.path.exists(directory): return None

        _, ext = os.path.splitext(current_file_path)
        ext = ext.lower()

        # glob 对括号等特殊字符支持不好，直接用 listdir
        try:
            all_files = os.listdir(directory)
        except Exception:
            return None

        candidates = []
        for f in all_files:
            if f.lower().endswith(ext):
                full_path = os.path.join(directory, f)
                if os.path.isfile(full_path):
                    candidates.append(full_path)
        
        if not candidates: return None
            
        # 文件名带时间戳，直接按文件名倒序排最准
        candidates.sort(key=lambda x: os.path.basename(x), reverse=True)
        
        latest_file = candidates[0]
        
        if os.path.normpath(latest_file) != os.path.normpath(current_file_path):
            return latest_file
        return None

    # UI 回调
    def browse_file(self):
        path = filedialog.askopenfilename(filetypes=[("文本文件", "*.txt")])
        if path:
            self.path_var.set(path)
            self.save_config()
            self.load_tree_only() 

    def browse_output(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_var.set(folder)
            self.save_config()

    def on_drop_files(self, event):
        try:
            files_raw = self.root.tk.splitlist(event.data)
            files = [f.strip('{}') for f in files_raw]
        except Exception:
            files = [event.data]

        valid_txt_files = [f for f in files if f.lower().endswith('.txt')]
        
        if valid_txt_files:
            dropped_file = valid_txt_files[0]
            self.path_var.set(dropped_file)
            self.log(f"[拖入] 已设置目录树文件: {dropped_file}")
            self.save_config() 
            self.load_tree_only() 
        else:
            self.log("[拖入] 拖入的文件不是 .txt 文件。")

    def log(self, text):
        if threading.current_thread() is threading.main_thread():
            is_at_bottom = True 
            try:
                scroll_y = self.log_text.yview() 
                is_at_bottom = scroll_y[1] > 0.99
            except tk.TclError:
                pass
            
            self.log_text.config(state='normal')
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self.log_text.insert(tk.END, f"[{ts}] {text}\n")
            
            if is_at_bottom:
                self.log_text.see(tk.END)
            self.log_text.config(state='disabled')
        else:
            self.root.after(0, self.log, text)

    # 读写配置
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
                self.auto_load_latest_var.set(config.get('auto_load_latest', True))
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
                'save_config': self.save_var.get(),
                'auto_load_latest': self.auto_load_latest_var.get()
            }
            try:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.log(f"[错误] 保存配置 {CONFIG_FILE} 失败: {e}")
        elif os.path.exists(CONFIG_FILE):
            try:
                os.remove(CONFIG_FILE)
            except Exception:
                pass

    def _backup_index_file(self, index_file_path):
        if not os.path.exists(index_file_path): return
        try:
            backup_path = os.path.join(script_dir, os.path.basename(index_file_path) + ".bak")
            shutil.copy2(index_file_path, backup_path)
            self.log(f"[索引] 已备份当前索引文件到: {backup_path}")
        except Exception as e:
            self.log(f"[错误] 备份索引文件失败: {e}")

    # 解析部分
    def read_text_file_with_fallback(self, path):
        for enc in ['utf-8', 'utf-16', 'utf-8-sig', 'gb18030', 'gbk']:
            try:
                with open(path, 'r', encoding=enc) as f:
                    return f.readlines()
            except UnicodeDecodeError:
                continue
        raise UnicodeDecodeError("read", b"", 0, 1, "文件编码错误，建议另存为 UTF-8")

    def parse_directory_tree(self, lines):
        paths = []
        stack = []
        processing = False
        start_keyword = self.start_keyword_var.get().strip()

        if not start_keyword:
            processing = True

        for line in lines:
            line = line.rstrip('\n\r')
            if not line.strip(): continue
            
            if start_keyword and not processing:
                if start_keyword in line:
                    stack = [] 
                    processing = True
                continue 
            
            if not processing: continue

            match = TREE_LINE_PATTERN.match(line)
            if match:
                prefix = match.group(1)
                name = match.group(2).strip()
                depth = len(prefix.replace(' ', ''))
                
                while len(stack) > depth:
                    stack.pop()

                if len(stack) <= depth:
                    while len(stack) < depth:
                        stack.append("") 
                    if stack and len(stack) == depth:
                        stack[-1] = name
                    else: 
                        stack.append(name)

                full_path = '/'.join([p for p in stack if p])
                
                lower_name = name.lower()
                is_media = False
                for ext in VIDEO_EXTS:
                    if lower_name.endswith(ext):
                        is_media = True
                        break
                
                if is_media and '.' in name.rsplit('/', 1)[-1]:
                    paths.append(full_path)
            
            elif processing and '|' not in line and '-' not in line:
                name = line.strip()
                if name:
                    lower_name = name.lower()
                    is_media = False
                    for ext in VIDEO_EXTS:
                        if lower_name.endswith(ext):
                            is_media = True
                            break
                    if is_media and not stack:
                        paths.append(name)
        return paths

    # 载入线程
    def _load_tree_blocking(self):
        input_path = self.path_var.get()
        if not input_path or not os.path.exists(input_path):
            self.log("[错误] 载入失败：目录树文件路径无效。")
            self.root.after(0, lambda: self.status_var.set("❌ 目录树文件路径无效！"))
            return None
            
        try:
            lines = self.read_text_file_with_fallback(input_path) 
            all_media_paths = self.parse_directory_tree(lines)
            
            folder_set = sorted(set(os.path.dirname(p) for p in all_media_paths if os.path.dirname(p)))
            if any(not os.path.dirname(p) for p in all_media_paths):
                 folder_set.insert(0, "") 
                 
            return (all_media_paths, folder_set)
        except Exception as e:
            self.log(f"[错误] 解析目录树失败: {e}")
            self.log(traceback.format_exc())
            self.root.after(0, lambda: self.status_var.set("❌ 解析失败！请检查文件编码或格式。"))
            return None

    def load_tree_only(self, callback=None):
        if not self._is_loading.acquire(blocking=False):
            self.log("[提示] 正在处理中，请稍候...")
            if callback: self.root.after(0, lambda: callback(False))
            return

        # 检查有没有新文件
        if self.auto_load_latest_var.get():
            current_path = self.path_var.get()
            latest_path = self._find_latest_file(current_path)
            if latest_path:
                latest_filename = os.path.basename(latest_path)
                if os.path.normpath(current_path) != os.path.normpath(latest_path):
                    self.log(f"检测到更新的目录树文件，已自动切换为 {latest_filename}")
                    self.path_var.set(latest_path)

        self.root.after(0, lambda: self.status_var.set("🔄 正在载入目录树..."))
        
        def worker():
            try:
                results = self._load_tree_blocking()
                if results is None:
                    if callback: self.root.after(0, lambda: callback(False))
                    return
                
                all_media_paths, folder_set = results

                def update_ui():
                    self.all_media_paths = all_media_paths
                    self.folder_choices = set(folder_set)
                    self.selected_folders = set() 
                    
                    self.log(f"[载入] 成功解析 {len(self.all_media_paths)} 个媒体文件，{len(folder_set)} 个文件夹。")
                    self.status_var.set(f"✅ 目录树载入完成，共 {len(self.all_media_paths)} 个文件。")
                    if callback: callback(True)

                self.root.after(0, update_ui)

            except Exception as e:
                def log_err():
                    self.log(f"[错误] 载入目录树时发生意外: {e}")
                    self.log(traceback.format_exc())
                    self.status_var.set("❌ 载入失败！请检查日志。")
                    if callback: callback(False)
                self.root.after(0, log_err)
            finally:
                self._is_loading.release()
        
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    # 目录多选窗
    def show_folder_selector(self):
        if not self.folder_choices:
            self.log("[提示] 文件夹列表为空，正在尝试自动载入...")
            def on_load(success):
                if success and self.folder_choices: self.show_folder_selector()
                elif not success: self.log("[错误] 自动载入失败，无法打开目录选择器。")
                else: self.log("[错误] 无法载入文件夹列表。请检查目录树文件。")
            self.load_tree_only(callback=on_load)
            return

        win = tk.Toplevel(self.root)
        win.title("选择要生成的目录")
        win.geometry("750x550")
        
        bottom = tk.Frame(win)
        bottom.pack(side='bottom', fill='x', pady=(5, 10))
        btns = tk.Frame(bottom)
        btns.pack()
        
        tk.Button(btns, text="确认生成", width=12, command=lambda: confirm()).pack(side='left', padx=10)
        tk.Button(btns, text="取消", width=12, command=win.destroy).pack(side='left', padx=10)

        # 搜索
        filter_frame = tk.LabelFrame(win, text="🔍 筛选目录", padx=10, pady=5)
        filter_frame.pack(side='top', fill='x', padx=10, pady=(10, 5)) 
        search_var = tk.StringVar()
        tk.Entry(filter_frame, textvariable=search_var, width=50).pack(side='left', fill='x', expand=True, padx=5)

        # 列表
        list_frame = tk.LabelFrame(win, text="📂 目录列表 (可多选)", padx=10, pady=10)
        list_frame.pack(side='top', fill='both', expand=True, padx=10, pady=5)

        sel_btns = tk.Frame(list_frame)
        sel_btns.pack(fill='x', pady=(0, 5))
        tk.Button(sel_btns, text="全选", width=10, command=lambda: listbox.select_set(0, tk.END)).pack(side='left', padx=5)
        tk.Button(sel_btns, text="全不选", width=10, command=lambda: listbox.select_clear(0, tk.END)).pack(side='left', padx=5)

        scrollbar = tk.Scrollbar(list_frame, orient='vertical')
        listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, selectmode='extended', height=20)
        scrollbar.config(command=listbox.yview)
        scrollbar.pack(side='right', fill='y')
        listbox.pack(side='left', fill='both', expand=True)

        sorted_folders = sorted(list(self.folder_choices))

        def populate(items):
            listbox.delete(0, tk.END)
            for i, f in enumerate(items):
                listbox.insert(tk.END, "[根目录]" if f == "" else f)
                if f in self.selected_folders: listbox.select_set(i)

        search_var.trace_add('write', lambda *args: populate([f for f in sorted_folders if search_var.get().lower() in f.lower()]))
        
        def confirm():
            sel = [listbox.get(i) for i in listbox.curselection()]
            real = {f if f != "[根目录]" else "" for f in sel}
            if not real:
                self.log("[提示] 你没有选择任何目录。")
                win.destroy()
                return
            self.selected_folders = real
            self.log(f"[选择] 已选择 {len(real)} 个目录准备生成。")
            win.destroy()
            self.start_generation(mode='single') 
        
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        populate(sorted_folders)
        win.transient(self.root)
        win.grab_set()
        self.root.wait_window(win)

    # 全量确认
    def confirm_and_start_full_generation(self):
        if not self.all_media_paths:
            self.log("[提示] 目录树未载入，正在尝试自动载入...")
            self.load_tree_only(callback=lambda s: self.confirm_and_start_full_generation() if s else None)
            return

        message = (f"您确定要执行 **全量生成** 吗？\n\n"
                   f"此操作将根据当前目录树文件 (共 {len(self.all_media_paths)} 个媒体文件) "
                   f"在输出目录中重新生成所有 STRM 文件。\n"
                   f"⚠️ 【警告】这会清除输出目录下旧的 STRM 索引并重新创建所有文件！")
        
        if messagebox.askyesno("全量生成确认", message):
            self.log("[确认] 用户已确认全量生成。")
            self.start_generation(mode='full')
        else:
            self.log("[取消] 用户取消了全量生成操作。")
            self.root.after(0, lambda: self.status_var.set("✅ 全量生成已取消。"))

    # 生成主逻辑
    def start_generation(self, mode='full'):
        self.last_mode = mode
        t = threading.Thread(target=self._worker_generate, args=(mode,))
        t.daemon = True
        t.start()

    def _worker_generate(self, mode):
        if not self._is_loading.acquire(blocking=False):
            self.log("[错误] 无法开始生成：当前正在进行其他操作。请稍后再试。")
            self.root.after(0, lambda: self.status_var.set("❌ 操作冲突，请等待完成"))
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
            
            if not input_path or not os.path.exists(input_path):
                self.log("[错误] 目录树文件路径无效！")
                self.root.after(0, lambda: self.status_var.set("❌ 目录树文件路径无效！"))
                return
            if not prefix:
                self.log("[错误] 请填写 openlist 链接前缀！")
                self.root.after(0, lambda: self.status_var.set("❌ 链接前缀为空！"))
                return
            if not output_dir:
                self.log("[错误] STRM 输出目录为空！")
                self.root.after(0, lambda: self.status_var.set("❌ STRM 输出目录为空！"))
                return
            os.makedirs(output_dir, exist_ok=True)

            if not self.all_media_paths:
                self.log("[提示] 缓存为空，正在自动载入目录树...")
                evt = threading.Event()
                def load_wrap():
                    def cb(s):
                        if not s: self.log("[错误] _worker_generate 自动载入失败。")
                        evt.set()
                    self._is_loading.release()
                    self.load_tree_only(callback=cb)
                self.root.after(0, load_wrap)
                evt.wait()
                if not self._is_loading.acquire(blocking=False): return
                if not self.all_media_paths: return

            if mode in ['full', 'increment']:
                self.selected_folders = self.folder_choices
                if mode == 'full':
                     self.log(f"[模式] 全量模式：将处理全部 {len(self.folder_choices)} 个文件夹。")
                elif mode == 'increment':
                     self.log(f"[模式] 增量模式：将处理全部 {len(self.folder_choices)} 个文件夹。")

            media_paths = [p for p in self.all_media_paths if os.path.dirname(p) in self.selected_folders or (os.path.dirname(p) == '' and '' in self.selected_folders)]
            
            if not media_paths:
                self.log("[提示] 没有在选定文件夹中找到符合条件的媒体文件。")
                self.root.after(0, lambda: self.status_var.set("⚠️ 没有符合条件的文件。"))
                return
                
            self.log(f"[过滤] 共有 {len(media_paths)} 个文件待处理...")

            files_to_gen = [] 
            index_file = os.path.join(output_dir, '.strm_index.json')
            old_index = {}
            new_index = {} 

            if mode == "increment":
                if os.path.exists(index_file):
                    try:
                        with open(index_file, 'r', encoding='utf-8') as f: old_index = json.load(f)
                        self.log(f"[索引] 成功载入旧索引，共 {len(old_index)} 项。")
                    except Exception as e:
                        self.log(f"[警告] 载入旧索引失败 ({e})，将视为全量操作。")
                
                for path in media_paths:
                    fp = trim_path_by_keyword(path, start_keyword)
                    new_index[fp] = path 

                added = [path for fp, path in new_index.items() if fp not in old_index]
                removed = [p for p in old_index.keys() if p not in new_index]

                self.log(f"[对比] 新增: {len(added)} 项, 移除: {len(removed)} 项。")

                if added or removed:
                    files_to_gen = self.preview_selection(added, removed)
                else:
                    self.log("[提示] 没有新增或删除项目，增量生成结束。")
                    self.root.after(0, lambda: self.status_var.set("✅ 增量生成完成，无需操作"))

            elif mode == "single":
                self.log("[模式] 选择目录生成，进行文件预览...")
                files_to_gen = self.preview_selection(media_paths, [])
            
            elif mode == "full":
                self.log("[模式] 全量生成，跳过预览，直接处理所有文件...")
                files_to_gen = media_paths 
            
            count = 0
            if not files_to_gen:
                self.log("[提示] 没有需要写入的文件。")
                self.root.after(0, lambda: self.status_var.set("✅ 完成，无需写入。"))
                return
                
            success_idx = {}
            idx_lock = threading.Lock()
             
            max_workers = min(64, max(4, (os.cpu_count() or 4) * 4))
            self.log(f"[多线程] 启用 {max_workers} 个并发线程进行 STRM 写入...")

            def write_task(mp):
                try:
                    base = os.path.basename(mp)
                    name_clean = re.sub(r'[\\/:*?"<>|]', '_', os.path.splitext(base)[0]).strip()
                    if not name_clean: name_clean = f"invalid_{int(time.time())}"
                    fname = name_clean + (ext if ext.startswith('.') else '.' + ext)
                    
                    tpath = trim_path_by_keyword(mp, start_keyword)
                    rel_dir = os.path.dirname(tpath).lstrip('/\\')
                    target = os.path.join(output_dir, rel_dir) if rel_dir else output_dir
                    
                    os.makedirs(target, exist_ok=True)
                    
                    url_part = tpath.replace('\\', '/').lstrip('/')
                    if encode_url:
                        url_part = '/'.join(urllib.parse.quote(p) for p in url_part.split('/'))
                    
                    full_url = f"{prefix}/{url_part}"
                    full_url = re.sub(r'(?<!:)/{2,}', '/', full_url)
                    
                    out_p = os.path.join(target, fname)
                    with open(out_p, 'w', encoding='utf-8') as f: f.write(full_url + '\n')
                    return f"[写入] {out_p} → {full_url}", 1, tpath
                except Exception as e:
                    return f"[失败] 写入 {mp} 错误: {e}", 0, None

            total = len(files_to_gen)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(write_task, p): p for p in files_to_gen}
                for i, fut in enumerate(as_completed(futures)):
                    err, ret, key = fut.result()
                    if ret:
                        count += 1
                        with idx_lock: success_idx[key] = True
                    else:
                        self.log(err)
                    
                    if (i+1) % 100 == 0 or (i+1) == total:
                        self.root.after(0, lambda c=count, t=total: self.status_var.set(f"🔄 写入中... {c}/{t}"))

            if mode == "full":
                try:
                    self._backup_index_file(index_file)
                    with open(index_file, 'w', encoding='utf-8') as f:
                        json.dump(success_idx, f, ensure_ascii=False, indent=2)
                    self.log(f"[索引] 全量模式：已为 {len(success_idx)} 个【成功写入】的文件保存索引。")
                except Exception as e:
                    self.log(f"[错误] 保存 'full' 模式索引失败: {e}")

            elif mode in ["single", "increment"]:
                if success_idx or (mode == "increment" and removed):
                    curr = {}
                    if os.path.exists(index_file):
                        try:
                            with open(index_file, 'r', encoding='utf-8') as f: curr = json.load(f)
                        except: pass
                    
                    final = curr.copy()
                    if mode == "increment":
                        for r in removed: 
                            if r in final: 
                                del final[r]
                                self.log(f"[清理] 已从索引中移除: {r}")
                    final.update(success_idx)
                    
                    if final != curr:
                        try:
                            self._backup_index_file(index_file)
                            with open(index_file, 'w', encoding='utf-8') as f:
                                json.dump(final, f, ensure_ascii=False, indent=2)
                            
                            if mode == "increment":
                                self.log(f"[索引] 增量模式：已【更新】全局索引 (新增 {len(success_idx)} 项，移除 {len(removed)} 项)。")
                            else:
                                self.log(f"[索引] 选择目录模式：已【增量更新】全局索引 (新增 {len(success_idx)} 项)。")
                        except Exception as e:
                            self.log(f"[错误] 保存 '{mode}' 模式索引失败: {e}")
                    else:
                        self.log("[提示] 索引未发生变化，无需保存。")
                else:
                    self.log(f"[提示] '{mode}' 模式完成。未写入文件，索引未更新。")

            self.log(f"[完成] 共生成 {count} 个 STRM 文件。")
            self.root.after(0, lambda: self.status_var.set(f"✅ 完成，生成 {count} 个文件。"))
            self.save_config(mode)

        except Exception as e:
            self.log(f"[异常] 生成过程中出现错误: {e}")
            self.log(traceback.format_exc())
            self.root.after(0, lambda: self.status_var.set("❌ 生成失败！"))
        finally:
            if self._is_loading.locked(): self._is_loading.release()

    # 预览弹窗
    def preview_selection(self, added, removed):
        evt = threading.Event()
        res = {'gen': list(added)} 

        def show():
            win = tk.Toplevel(self.root)
            win.title("选择生成项")
            win.geometry("820x520")

            tk.Label(win, text=f"新增: {len(added)}  移除: {len(removed)}").pack(anchor='w', padx=10, pady=6)
            
            btn_frame = tk.Frame(win)
            btn_frame.pack(side='bottom', pady=6) 
            btns = tk.Frame(btn_frame)
            btns.pack()

            if removed:
                f_del = tk.LabelFrame(win, text="已移除项（仅参考，这些 STRM 文件可能需要手动删除）", padx=6, pady=6)
                f_del.pack(side='bottom', fill='x', expand=False, padx=10, pady=6)
                txt = scrolledtext.ScrolledText(f_del, width=96, height=8)
                txt.pack(fill='both', expand=True)
                for r in removed: txt.insert(tk.END, f"{r}\n")
                txt.configure(state='disabled')

            f_add = tk.LabelFrame(win, text="可选生成项 (勾选的项目将被生成，未勾选的项目下次增量会重试)", padx=6, pady=6)
            f_add.pack(fill='both', expand=True, padx=10, pady=6)
            
            cvs = tk.Canvas(f_add)
            sb = tk.Scrollbar(f_add, orient="vertical", command=cvs.yview)
            inn = tk.Frame(cvs)
            
            inn.bind("<Configure>", lambda e: cvs.configure(scrollregion=cvs.bbox("all")))
            cvs.create_window((0,0), window=inn, anchor='nw')
            cvs.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            cvs.pack(side="left", fill='both', expand=True)

            vars_map = {}
            if len(added) > 5000:
                tk.Label(inn, text=f"项目过多 ({len(added)} 个)，默认全部生成。", fg='red').pack(pady=20)
            else:
                def loader(idx=0):
                    cnt = 0
                    while cnt < 200 and idx < len(added):
                        p = added[idx]
                        v = tk.BooleanVar(value=True)
                        tk.Checkbutton(inn, text=p, variable=v, anchor='w').pack(anchor='w', fill='x')
                        vars_map[p] = v
                        cnt += 1; idx += 1
                    if idx < len(added): win.after(1, lambda: loader(idx))
                loader(0)

            def ok():
                if len(added) <= 5000:
                    res['gen'] = [p for p,v in vars_map.items() if v.get()]
                win.destroy(); evt.set()
                
            def cancel():
                res['gen'] = []; win.destroy(); evt.set()
                
            tk.Button(btns, text="确认生成所选项", command=ok).pack(side='left', padx=8)
            tk.Button(btns, text="取消（不生成）", command=cancel).pack(side='left', padx=8)

            win.protocol("WM_DELETE_WINDOW", cancel)
            win.transient(self.root)
            win.grab_set()
            self.root.wait_window(win)
            if not evt.is_set(): res['gen'] = []; evt.set()

        self.root.after(0, show)
        evt.wait()
        return res['gen']

if __name__ == "__main__":
    root = TkinterDnD.Tk()
    StrmGeneratorApp(root)
    root.mainloop()
