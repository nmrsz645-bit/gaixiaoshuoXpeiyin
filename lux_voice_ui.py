"""LuxTTS local voice selection and reference import dialog."""
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import lux_voice_store


def open_lux_voice_manager(parent):
    import voice_monitor

    config = voice_monitor.load_config()
    dialog = tk.Toplevel(parent)
    dialog.title("LuxTTS 音色与声音克隆")
    dialog.geometry("540x450")
    dialog.transient(parent)
    dialog.grab_set()
    page = ttk.Frame(dialog, padding=14)
    page.pack(fill="both", expand=True)
    ttk.Label(page, text="随机模式可选择多个音色；固定模式选择一个音色。", wraplength=500).pack(anchor="w")
    listing = tk.Listbox(page, selectmode="extended", exportselection=False, height=12)
    listing.pack(fill="both", expand=True, pady=10)
    random_voice = tk.BooleanVar(value=config.get("lux_use_random_voice", True))
    ttk.Checkbutton(page, text="每本书随机选择已勾选的音色", variable=random_voice).pack(anchor="w")
    status = tk.StringVar(value="导入清晰人声 MP3/WAV，使用前五秒建立本地参考音色。")
    ttk.Label(page, textvariable=status, wraplength=500).pack(anchor="w", pady=8)
    controls = ttk.Frame(page)
    controls.pack(fill="x")
    codes = []

    def refresh(selected=None):
        try:
            profiles = lux_voice_store.list_profiles(config)
        except ValueError as error:
            messagebox.showerror("无法读取音色", str(error), parent=dialog)
            return
        codes[:] = profiles
        listing.delete(0, "end")
        chosen = set(selected if selected is not None else config.get("lux_random_voices", lux_voice_store.DEFAULT_VOICES))
        if not random_voice.get() and selected is None:
            chosen = {config.get("lux_voice", "zh-CN-XiaoxiaoNeural")}
        for index, code in enumerate(codes):
            profile = profiles[code]
            suffix = "（本地参考）" if profile["builtin"] else "（私人克隆）"
            listing.insert("end", profile["name"] + suffix)
            if code in chosen:
                listing.selection_set(index)

    def save():
        chosen = [codes[index] for index in listing.curselection()]
        if not chosen or not random_voice.get() and len(chosen) != 1:
            messagebox.showerror("无法保存", "随机模式至少选择一个音色；固定模式请选择一个音色。", parent=dialog)
            return
        active = voice_monitor.load_config()
        active.update(lux_use_random_voice=random_voice.get(), lux_random_voices=chosen, lux_voice=chosen[0])
        voice_monitor.save_config(active)
        if hasattr(parent, "log"):
            parent.log(f"LuxTTS 音色已保存，已选 {len(chosen)} 个。")
        dialog.destroy()

    def imported(profile=None, error=None):
        import_button.configure(state="normal")
        save_button.configure(state="normal")
        if error is not None:
            status.set("导入失败，请检查参考音频。")
            messagebox.showerror("声音克隆失败", str(error), parent=dialog)
            return
        refresh([profile["id"]])
        random_voice.set(False)
        status.set(f"已建立“{profile['name']}”，保存后可用主页面“试听 LuxTTS”试听。")

    def import_clone():
        source = filedialog.askopenfilename(parent=dialog, title="选择参考人声", filetypes=[("人声音频", "*.mp3 *.wav")])
        if not source:
            return
        name = simpledialog.askstring("声音克隆", "请输入新音色名称", parent=dialog)
        if name is None:
            return
        import_button.configure(state="disabled")
        save_button.configure(state="disabled")
        status.set("正在提取参考人声……")

        def work():
            try:
                profile = lux_voice_store.import_voice(source, name, config)
            except Exception as error:
                dialog.after(0, lambda captured=error: imported(error=captured))
            else:
                dialog.after(0, lambda: imported(profile=profile))

        threading.Thread(target=work, daemon=True).start()

    import_button = ttk.Button(controls, text="导入音频建立克隆音色", command=import_clone)
    import_button.pack(side="left")
    save_button = ttk.Button(controls, text="保存音色选择", command=save)
    save_button.pack(side="right")
    refresh()
