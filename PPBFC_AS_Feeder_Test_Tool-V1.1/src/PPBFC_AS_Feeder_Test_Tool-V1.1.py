# coding: utf-8
import tkinter as tk
from tkinter import ttk, scrolledtext
import serial
import serial.tools.list_ports
import threading
import time
from datetime import datetime
import os
import sys


def get_resource_path(relative_path):
    base = getattr(sys, "_MEIPASS", os.path.dirname(__file__))
    return os.path.join(base, relative_path)


def _noop_logger(_text):
    pass


def _decode_serial_chunk(data):
    if not data:
        return ""
    return data.decode("utf-8", errors="ignore").replace("\r", "")


def _decode_serial_text(data):
    return _decode_serial_chunk(data).strip()


def _buffer_incoming_lines(pending_text, data, log_incoming):
    if not log_incoming or not data:
        return pending_text

    pending_text += _decode_serial_chunk(data)
    while "\n" in pending_text:
        line, pending_text = pending_text.split("\n", 1)
        line = line.strip()
        if line:
            log_incoming(line)

    return pending_text


def _flush_incoming_lines(pending_text, log_incoming):
    if log_incoming:
        line = pending_text.strip()
        if line:
            log_incoming(line)
    return ""


def _read_available_bytes(ser, rx_buf):
    n = getattr(ser, "in_waiting", 0)
    if n <= 0:
        return b""

    data = ser.read(n)
    rx_buf.extend(data)

    return data


def _write_serial_bytes(ser, payload, log_outgoing=None):
    ser.write(payload)
    ser.flush()

    if log_outgoing and payload.endswith(b"\n"):
        text = _decode_serial_text(payload)
        if text:
            log_outgoing(text)

CALIBRATION_STOP_TOKEN = b"__cal_finish__"
CALIBRATION_SEND_CHUNK = b"U" * 16

def perform_bfc_calibration(
    ser,
    loop_count,
    forced_reset=False,
    log_system=None,
    log_incoming=None,
    log_outgoing=None,
    time_fn=None,
    sleep_fn=None,
    forced_reset_delay_s=3.0,
    prompt_wait_s=0.5,
    max_send_time_s=2.4,
    final_read_time_s=1.0,
    settle_delay_s=0.2,
    post_loop_delay_s=0.5,
    final_quit_delay_s=1.0,
    inter_loop_delay_s=0.2,
):
    log_system = log_system or _noop_logger
    log_incoming = log_incoming or _noop_logger
    log_outgoing = log_outgoing or _noop_logger
    time_fn = time_fn or time.monotonic
    sleep_fn = sleep_fn or time.sleep

    if forced_reset:
        log_system("Forced calibration requested: sending C998 calibration reset command.")
        # log_system("(If the feature is unavailable, please update to the latest BFC firmware.)\n")
        _write_serial_bytes(ser, b"C998\n", log_outgoing)
        sleep_fn(forced_reset_delay_s)

    if loop_count <= 0:
        log_system("BFC calibration skipped: loop count is 0.")
        return 0

    completed_loops = 0

    try:
        log_system("(If the feature is not working, please update to the latest BFC firmware.)\n")
        for loop_index in range(loop_count):
            log_system(f"BFC calibration loop {loop_index + 1}/{loop_count} started.")
            sleep_fn(inter_loop_delay_s)
            incoming_text_buf = ""

            ser.reset_input_buffer()
            ser.reset_output_buffer()

            _write_serial_bytes(ser, b"C999\n", log_outgoing)

            prompt_buf = bytearray()
            t0 = time_fn()
            while time_fn() - t0 < prompt_wait_s:
                data = _read_available_bytes(ser, prompt_buf)
                incoming_text_buf = _buffer_incoming_lines(incoming_text_buf, data, log_incoming)
                sleep_fn(0.01)

            log_system(" Start BFC Calibration...")
            rx_buf = bytearray(prompt_buf)
            got_finish = False
            t_start = time_fn()

            while time_fn() - t_start < max_send_time_s:
                ser.write(CALIBRATION_SEND_CHUNK)
                ser.flush()
                sleep_fn(0.03)
                data = _read_available_bytes(ser, rx_buf)
                incoming_text_buf = _buffer_incoming_lines(incoming_text_buf, data, log_incoming)

                if CALIBRATION_STOP_TOKEN in bytes(rx_buf).lower():
                    log_system("STOP calibration.")
                    got_finish = True
                    break

            ser.reset_output_buffer()
            sleep_fn(settle_delay_s)

            final_buf = bytearray()
            t0 = time_fn()
            while time_fn() - t0 < final_read_time_s:
                data = _read_available_bytes(ser, final_buf)
                incoming_text_buf = _buffer_incoming_lines(incoming_text_buf, data, log_incoming)
                if not data:
                    sleep_fn(0.01)

            incoming_text_buf = _flush_incoming_lines(incoming_text_buf, log_incoming)

            if not got_finish:
                log_system("WARNING: calibration timeout.\n")

            sleep_fn(post_loop_delay_s)
            completed_loops += 1
    finally:
        _write_serial_bytes(ser, b"C997\n", log_outgoing)
        sleep_fn(final_quit_delay_s)
        _write_serial_bytes(ser, b"M999\n", log_outgoing)

    return completed_loops

class SerialTool:
    def __init__(self, root):
        self.root = root
        self.root.title("PPBFC AS Feeder Test Tool V1.1 - Pandaplacer")
        self.root.geometry("1280x720")
        icon_path = get_resource_path("logo.png")
        img = tk.PhotoImage(file=icon_path)
        self.root.iconphoto(True, img)
        
        # --- Color Palette ---
        self.clr_bg = "#F8F9FA"       
        self.clr_widget_bg = "#FFFFFF" 
        self.clr_text = "#212529"      
        self.clr_border = "#DEE2E6"    
        self.clr_accent = "#007BFF"    
        self.clr_success = "#28A745"   
        self.clr_send_default = "#CA7E40"
        self.clr_send_active = "#CA6716"
        self.clr_btn_angle = "#17A2B8"   
        self.clr_btn_single = "#E83E8C"  
        self.clr_btn_repeat = "#6F42C1"  
        self.clr_btn_repeat_active = "#D630EC" 
        self.clr_btn_get = "#0056b3"     
        self.clr_btn_update = "#218838"  
        self.clr_btn_default = "#6C757D" 
        self.clr_btn_activate = "#FFC107" 
        self.clr_btn_calibration = "#E83E8C"
        self.clr_disabled_bg = "#E9ECEF" 
        self.clr_disabled_fg = "#ADB5BD" 
        self.clr_info = "#6C757D"      
        self.clr_select = "#E1F0FF"    
        self.clr_clear_bg = "#5C646D" 

        self.root.configure(bg=self.clr_bg)

        # Serial object and status
        self.ser = None
        self.is_receiving = False
        self.is_repeating = False  
        self.is_calibrating = False
        self.rx_paused = False
        
        # Variables for Preset Logic
        self.var_board_addr = tk.IntVar(value=0)
        self.var_port_idx = tk.IntVar(value=0)
        self.var_port_n_str = tk.StringVar(value="Port-N=0")
        self.var_controller_count = tk.IntVar(value=4)
        self.var_forced_calibration = tk.BooleanVar(value=False)
        self.port_n = 0

        # Input Validation Register
        self.vcmd_int = (self.root.register(self.validate_int_input), '%P')
        
        self.setup_ui()
        self.refresh_ports() 
        self.port_combo.set('')
        self.update_button_states(False)
        self.rx_buffer = ""

        # Bind Closing Event
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def validate_int_input(self, new_value):
        if new_value == "": return True
        return new_value.isdigit()

    def on_angle_focus_out(self, event, min_val=0, max_val=180):
        widget = event.widget
        val_str = widget.get()
        if not val_str:
            widget.insert(0, str(max_val))
            return
        try:
            val = int(val_str)
            if val < min_val:
                widget.delete(0, tk.END)
                widget.insert(0, str(min_val))
            elif val > max_val:
                widget.delete(0, tk.END)
                widget.insert(0, str(max_val))
        except ValueError:
            widget.delete(0, tk.END)
            widget.insert(0, str(max_val))

    def on_time_focus_out(self, event):
        """Handle 1200-99999 ms range validation"""
        val_str = self.ent_time.get()
        try:
            val = int(val_str) if val_str else 0
            if val < 1200:
                self.ent_time.delete(0, tk.END)
                self.ent_time.insert(0, "1200")
            elif val > 99999:
                self.ent_time.delete(0, tk.END)
                self.ent_time.insert(0, "99999")
        except ValueError:
            self.ent_time.delete(0, tk.END)
            self.ent_time.insert(0, "1200")

    def setup_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TLabel", background=self.clr_bg, foreground=self.clr_text, font=("Segoe UI", 10))
        style.configure("TLabelframe", background=self.clr_bg, bordercolor=self.clr_border)
        style.configure("TLabelframe.Label", background=self.clr_bg, foreground=self.clr_text, font=("Segoe UI", 10, "bold"))
        style.configure("TCombobox", fieldbackground=self.clr_widget_bg, background=self.clr_bg)

        style.configure("Vertical.TScrollbar", 
                        relief="flat",
                        borderwidth=0,
                        gripcount=0,         
                        background="#c1c1c1",     
                        darkcolor="#c1c1c1",      
                        lightcolor="#c1c1c1",     
                        troughcolor=self.clr_bg,      
                        bordercolor=self.clr_bg,    
                        arrowcolor="#666666",       
                        arrowsize=1)               
        style.configure("Vertical.TScrollbar.uparrow", relief="flat", background=self.clr_bg, borderwidth=0)
        style.configure("Vertical.TScrollbar.downarrow", relief="flat", background=self.clr_bg, borderwidth=0)

        main_container = tk.Frame(self.root, bg=self.clr_bg, padx=15, pady=15)
        main_container.pack(fill=tk.BOTH, expand=True)

        # ================== LEFT PANEL (500px Fixed) ==================
        left_panel = tk.Frame(main_container, width=500, bg=self.clr_bg)
        left_panel.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 15))
        left_panel.pack_propagate(False)

        # --- Section 1: Connection ---
        config_frame = ttk.LabelFrame(left_panel, text="SERIAL CONNECTION", padding=15)
        config_frame.pack(fill=tk.X)
        
        ttk.Label(config_frame, text="Port:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.port_combo = ttk.Combobox(config_frame, state="readonly", postcommand=self.refresh_ports, height=20)
        self.port_combo.grid(row=0, column=1, sticky=tk.EW, padx=(10, 15), pady=5)
        
        ttk.Label(config_frame, text="Baud Rate:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.baud_combo = ttk.Combobox(config_frame, values=[4800, 9600, 19200, 38400, 57600, 115200], state="readonly", height=20)
        self.baud_combo.set(19200)
        self.baud_combo.grid(row=1, column=1, sticky=tk.EW, padx=(10, 15), pady=5)

        btn_conn_container = tk.Frame(config_frame, width=124, bg=self.clr_bg)
        btn_conn_container.grid(row=0, column=2, rowspan=2, sticky="nsew", pady=5)
        btn_conn_container.pack_propagate(False)
        self.btn_connect = tk.Button(btn_conn_container, text="Connect", command=self.toggle_serial, bg=self.clr_accent, fg="white", relief=tk.FLAT, font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.btn_connect.pack(fill=tk.BOTH, expand=True)
        config_frame.columnconfigure(1, weight=1)

        # --- Section 2: Presets ---
        preset_frame = ttk.LabelFrame(left_panel, text="PPBFC AS FEEDER COMMANDS", padding=15)
        preset_frame.pack(fill=tk.BOTH, expand=True, pady=(15, 0))

        # Addressing
        addr_frame = tk.Frame(preset_frame, bg=self.clr_bg)
        addr_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(addr_frame, text="BoardAddr:").grid(row=0, column=0, sticky="w")
        self.cb_board = ttk.Combobox(addr_frame, textvariable=self.var_board_addr, values=list(range(8)), state="readonly", width=5, height=20)
        self.cb_board.grid(row=0, column=1, padx=(5, 0))
        ttk.Label(addr_frame, text="Port:").grid(row=0, column=2, padx=(20, 5))
        self.cb_port = ttk.Combobox(addr_frame, textvariable=self.var_port_idx, values=list(range(13)), state="readonly", width=5, height=20)
        self.cb_port.grid(row=0, column=3)
        self.lbl_port_n = ttk.Label(addr_frame, textvariable=self.var_port_n_str, font=("Segoe UI", 10, "bold"), foreground=self.clr_accent)
        self.lbl_port_n.grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.cb_board.bind("<<ComboboxSelected>>", self.update_port_n_display)
        self.cb_port.bind("<<ComboboxSelected>>", self.update_port_n_display)

        tk.Frame(preset_frame, height=1, bg=self.clr_border).pack(fill=tk.X, pady=15)

        # Angle Control
        angle_frame = tk.Frame(preset_frame, bg=self.clr_bg)
        angle_frame.pack(fill=tk.X)
        btn_angle_cont = tk.Frame(angle_frame, width=124, height=26, bg=self.clr_bg)
        btn_angle_cont.pack(side=tk.LEFT); btn_angle_cont.pack_propagate(False)
        self.btn_set_angle = tk.Button(btn_angle_cont, text="Set Angle to", command=self.send_angle_command, bg=self.clr_btn_angle, fg="white", relief=tk.FLAT, font=("Segoe UI", 9))
        self.btn_set_angle.pack(fill=tk.BOTH, expand=True)
        self.ent_angle = tk.Entry(angle_frame, width=8, justify='center', font=("Consolas", 10), relief=tk.FLAT, highlightthickness=1, highlightbackground=self.clr_border, validate="key", validatecommand=self.vcmd_int)
        self.ent_angle.insert(0, "180"); self.ent_angle.pack(side=tk.LEFT, padx=(10, 0), ipady=2)
        self.ent_angle.bind("<FocusOut>", lambda e: self.on_angle_focus_out(e, 0, 180))

        tk.Frame(preset_frame, height=1, bg=self.clr_border).pack(fill=tk.X, pady=15)

        # Advance Control
        adv_frame = tk.Frame(preset_frame, bg=self.clr_bg)
        adv_frame.pack(fill=tk.X)
        adv_row_h = tk.Frame(adv_frame, bg=self.clr_bg); adv_row_h.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(adv_row_h, text="Advance distance (mm):").pack(side=tk.LEFT)
        self.cb_distance = ttk.Combobox(adv_row_h, values=[4, 8, 12, 16, 20, 24], state="readonly", width=5, height=20)
        self.cb_distance.set(4); self.cb_distance.pack(side=tk.LEFT, padx=10)
        
        btns_grid = tk.Frame(adv_frame, bg=self.clr_bg); btns_grid.pack(fill=tk.X)
        b_s_c = tk.Frame(btns_grid, width=124, height=26, bg=self.clr_bg); b_s_c.grid(row=0, column=0, sticky="w", pady=2); b_s_c.pack_propagate(False)
        self.btn_single_adv = tk.Button(b_s_c, text="Single Advance", command=self.send_single_advance, bg=self.clr_btn_single, fg="white", relief=tk.FLAT, font=("Segoe UI", 9))
        self.btn_single_adv.pack(fill=tk.BOTH, expand=True)
        
        b_a_c = tk.Frame(btns_grid, width=124, height=26, bg=self.clr_bg); b_a_c.grid(row=0, column=2, sticky="e", pady=2); b_a_c.pack_propagate(False)
        self.btn_activate = tk.Button(b_a_c, text="Activate", command=self.send_activate_command, bg=self.clr_btn_activate, fg="black", relief=tk.FLAT, font=("Segoe UI", 9, "bold"))
        self.btn_activate.pack(fill=tk.BOTH, expand=True)
        
        b_r_c = tk.Frame(btns_grid, width=124, height=26, bg=self.clr_bg); b_r_c.grid(row=1, column=0, sticky="w", pady=2); b_r_c.pack_propagate(False)
        self.btn_repeat_adv = tk.Button(b_r_c, text="Repeat Advance", command=self.toggle_repeat_advance, bg=self.clr_btn_repeat, fg="white", relief=tk.FLAT, font=("Segoe UI", 9))
        self.btn_repeat_adv.pack(fill=tk.BOTH, expand=True)
        
        t_f = tk.Frame(btns_grid, bg=self.clr_bg); t_f.grid(row=1, column=1, sticky="w", padx=10)
        self.ent_time = tk.Entry(t_f, width=6, justify='center', font=("Consolas", 10), relief=tk.FLAT, highlightthickness=1, highlightbackground=self.clr_border, validate="key", validatecommand=self.vcmd_int)
        self.ent_time.insert(0, "3000"); self.ent_time.pack(side=tk.LEFT, ipady=2)
        self.ent_time.bind("<FocusOut>", self.on_time_focus_out) 
        ttk.Label(t_f, text="ms").pack(side=tk.LEFT, padx=5)

        b_d_c = tk.Frame(btns_grid, width=124, height=26, bg=self.clr_bg); b_d_c.grid(row=1, column=2, sticky="e", pady=2); b_d_c.pack_propagate(False)
        self.btn_deactivate = tk.Button(b_d_c, text="Deactivate", command=self.send_deactivate_command, bg=self.clr_btn_default, fg="white", relief=tk.FLAT, font=("Segoe UI", 9))
        self.btn_deactivate.pack(fill=tk.BOTH, expand=True)
        btns_grid.columnconfigure(2, weight=1)

        tk.Frame(preset_frame, height=1, bg=self.clr_border).pack(fill=tk.X, pady=15)

        # Extended Settings
        ext_frame = tk.Frame(preset_frame, bg=self.clr_bg); ext_frame.pack(fill=tk.X)
        self.btn_get_all = tk.Button(ext_frame, text="Get All Settings", command=self.send_get_settings, bg=self.clr_btn_get, fg="white", relief=tk.FLAT, font=("Segoe UI", 9))
        self.btn_get_all.pack(fill=tk.X, pady=(0, 5))
        r2_f = tk.Frame(ext_frame, height=26, bg=self.clr_bg); r2_f.pack(fill=tk.X, pady=(0, 10))
        b_def_c = tk.Frame(r2_f, width=124, height=26, bg=self.clr_bg); b_def_c.pack(side=tk.RIGHT); b_def_c.pack_propagate(False)
        self.btn_default = tk.Button(b_def_c, text="Default", command=self.reset_to_defaults, bg=self.clr_btn_default, fg="white", relief=tk.FLAT, font=("Segoe UI", 9))
        self.btn_default.pack(fill=tk.BOTH, expand=True)
        self.btn_update_n = tk.Button(r2_f, text="Update Port-N Settings :", command=self.send_update_command, bg=self.clr_btn_update, fg="white", relief=tk.FLAT, font=("Segoe UI", 9))
        self.btn_update_n.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        p_grid = tk.Frame(ext_frame, bg=self.clr_bg); p_grid.pack(fill=tk.X)
        self.param_entries = {}
        params_def = [('A', 180, 0, 180), ('B', 120, 0, 180), ('C', 58, 0, 180), ('F', 4, None, None), ('U', 480, None, None), ('V', 500, None, None), ('W', 2500, None, None), ('X', 1, None, None)]
        for i, (lbl, default, min_v, max_v) in enumerate(params_def):
            r, c = i // 4, i % 4
            p_c = tk.Frame(p_grid, bg=self.clr_bg); p_c.grid(row=r, column=c, sticky="w", padx=2, pady=5)
            tk.Label(p_c, text=lbl, bg=self.clr_bg, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
            ent = tk.Entry(p_c, width=5, justify='center', font=("Consolas", 9), relief=tk.FLAT, highlightthickness=1, highlightbackground=self.clr_border, validate="key", validatecommand=self.vcmd_int)
            ent.insert(0, str(default)); ent.pack(side=tk.LEFT, padx=(2, 0))
            if min_v is not None: ent.bind("<FocusOut>", lambda e, mn=min_v, mx=max_v: self.on_angle_focus_out(e, mn, mx))
            self.param_entries[lbl] = ent

        calib_frame = ttk.LabelFrame(left_panel, text="BFC CONTROLLER CALIBRATION", padding=15)
        calib_frame.pack(fill=tk.X, pady=(15, 0))

        calib_row = tk.Frame(calib_frame, bg=self.clr_bg)
        calib_row.pack(fill=tk.X)
        ttk.Label(calib_row, text="Controller Count").pack(side=tk.LEFT)
        self.cb_controller_count = ttk.Combobox(
            calib_row,
            textvariable=self.var_controller_count,
            values=list(range(9)),
            state="readonly",
            width=5,
            height=20,
        )
        self.cb_controller_count.pack(side=tk.LEFT, padx=(10, 15))
        calib_btn_cont = tk.Frame(calib_row, height=26, bg=self.clr_bg)
        calib_btn_cont.pack(side=tk.LEFT, fill=tk.X, expand=True)
        calib_btn_cont.pack_propagate(False)
        self.btn_bfc_calibrate = tk.Button(
            calib_btn_cont,
            text="Auto Calibration",
            command=self.start_bfc_calibration,
            bg=self.clr_btn_calibration,
            fg="white",
            relief=tk.FLAT,
            font=("Segoe UI", 9, "bold"),
        )
        self.btn_bfc_calibrate.pack(fill=tk.BOTH, expand=True)
        forced_frame = tk.Frame(calib_row, bg=self.clr_bg)
        forced_frame.pack(side=tk.LEFT, padx=(12, 0))
        self.chk_forced_calibration = tk.Checkbutton(
            forced_frame,
            variable=self.var_forced_calibration,
            bg=self.clr_bg,
            activebackground=self.clr_bg,
            selectcolor=self.clr_widget_bg,
            highlightthickness=0,
            bd=0,
            relief=tk.FLAT,
            padx=0,
        )
        self.chk_forced_calibration.pack(side=tk.LEFT)
        ttk.Label(forced_frame, text="Forced").pack(side=tk.LEFT, padx=(2, 0))

        # ================== RIGHT PANEL ==================
        right_panel = tk.Frame(main_container, bg=self.clr_bg)
        right_panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        log_wrapper = tk.Frame(right_panel, bg=self.clr_bg, padx=0, pady=0)
        log_wrapper.place(relx=0, rely=0, relwidth=1, relheight=0.8)
        
        self.log_display = scrolledtext.ScrolledText(
            log_wrapper, wrap=tk.WORD, font=("Consolas", 10), 
            bg=self.clr_widget_bg, relief=tk.FLAT, highlightthickness=1, 
            highlightbackground=self.clr_border, state=tk.DISABLED,
            padx=10, pady=10 
        )
        self.log_display.pack(fill=tk.BOTH, expand=True)
        
        self.btn_clear = tk.Button(log_wrapper, text="CLEAR LOG", command=self.clear_logs, bg=self.clr_clear_bg, fg="white", relief=tk.FLAT, font=("Segoe UI", 8, "bold"), padx=10, cursor="hand2")
        self.btn_clear.place(relx=0.5, rely=0.055, anchor="n")

        self.log_display.tag_configure("incoming_ts", foreground=self.clr_accent, font=("Consolas", 9, "bold"))
        self.log_display.tag_configure("outgoing_ts", foreground=self.clr_success, font=("Consolas", 9, "bold"), justify='right')
        self.log_display.tag_configure("outgoing_msg", justify='right')
        self.log_display.tag_configure("sys_info", foreground=self.clr_info, font=("Consolas", 9, "italic"), justify='center')

        input_inner = tk.Frame(right_panel, bg=self.clr_bg)
        input_inner.place(relx=0, rely=0.8, relwidth=1, relheight=0.2)
        self.btn_send = tk.Button(input_inner, text="SEND\n(Ctrl+Enter)", command=self.send_data, bg=self.clr_send_default, fg="white", relief=tk.FLAT, padx=20, font=("Segoe UI", 9, "bold"))
        self.btn_send.pack(side=tk.RIGHT, fill=tk.Y, pady=(15,0))
        self.input_text = scrolledtext.ScrolledText(input_inner, font=("Consolas", 10), bg=self.clr_widget_bg, relief=tk.FLAT, highlightthickness=1, highlightbackground=self.clr_border)
        self.input_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 15), pady=(15,0))
        self.input_text.bind("<Control-Return>", lambda e: self.send_data())

        # ---  ---
        self.action_btns = [
            self.btn_set_angle, self.btn_single_adv, self.btn_activate, 
            self.btn_deactivate, self.btn_get_all, self.btn_default, 
            self.btn_update_n, self.btn_bfc_calibrate, self.btn_send
        ]

    # ------------------ Logic Backend ------------------
    def refresh_ports(self):
        ports = [port.device for port in serial.tools.list_ports.comports()]
        current_selection = self.port_combo.get()
        self.port_combo['values'] = ports
        if current_selection and current_selection not in ports:
            self.port_combo.set('')

    def update_button_states(self, is_connected):
        state = tk.NORMAL if is_connected else tk.DISABLED

        config_state = "disabled" if is_connected else "readonly"
        self.port_combo.config(state=config_state)
        self.baud_combo.config(state=config_state)
        self.cb_controller_count.config(state="readonly" if is_connected else tk.DISABLED)
        self.chk_forced_calibration.config(state=state)

        color_map = {
            self.btn_set_angle: self.clr_btn_angle, self.btn_single_adv: self.clr_btn_single,
            self.btn_repeat_adv: self.clr_btn_repeat, self.btn_activate: self.clr_btn_activate,
            self.btn_deactivate: self.clr_btn_default, self.btn_get_all: self.clr_btn_get,
            self.btn_update_n: self.clr_btn_update, self.btn_default: self.clr_btn_default, 
            self.btn_bfc_calibrate: self.clr_btn_calibration, self.btn_send: self.clr_send_default
        }
        for btn in color_map:
            clr = color_map[btn]
            fg = "black" if btn == self.btn_activate and is_connected else "white"
            btn.config(state=state, bg=clr if is_connected else self.clr_disabled_bg, fg=fg if is_connected else self.clr_disabled_fg)

    def set_calibration_state(self, is_running):
        self.is_calibrating = is_running

        if is_running:
            self.btn_connect.config(state=tk.DISABLED, bg=self.clr_disabled_bg, fg=self.clr_disabled_fg)
            self.cb_controller_count.config(state=tk.DISABLED)
            self.chk_forced_calibration.config(state=tk.DISABLED)
            for btn in self.action_btns:
                btn.config(state=tk.DISABLED, bg=self.clr_disabled_bg, fg=self.clr_disabled_fg)
            self.btn_bfc_calibrate.config(text="Calibrating...")
            return

        is_connected = bool(self.ser and self.ser.is_open)
        self.btn_bfc_calibrate.config(text="Auto Calibration")
        self.btn_connect.config(
            state=tk.NORMAL,
            bg=self.clr_success if is_connected else self.clr_accent,
            fg="white",
        )
        self.update_button_states(is_connected)

    def update_port_n_display(self, event=None):
        try:
            self.port_n = self.var_board_addr.get() * 100 + self.var_port_idx.get()
            self.var_port_n_str.set(f"Port-N={self.port_n}")
        except: self.var_port_n_str.set("Port-N=Error")

    def clear_logs(self):
        self.log_display.config(state=tk.NORMAL)
        self.log_display.delete("1.0", tk.END)
        self.log_display.config(state=tk.DISABLED)

    def reset_to_defaults(self):
        defaults = {'A': '180', 'B': '120', 'C': '58', 'F': '4', 'U': '480', 'V': '500', 'W': '2500', 'X': '1'}
        for k, v in defaults.items():
            self.param_entries[k].delete(0, tk.END)
            self.param_entries[k].insert(0, v)
        self.write_sys_log("Parameters reset to default values.")

    def send_angle_command(self):
        if not self.ser or not self.ser.is_open: return
        angle = self.ent_angle.get()
        cmds = ["M610 S1", "M611 S0", f"M611 N{self.port_n} S1", f"M603 N{self.port_n} A{angle}"]
        self.write_sys_log(f"Command: Set Angle {angle} for Port-N={self.port_n}")
        threading.Thread(target=self._execute_sequence, args=(cmds, 300), daemon=True).start()

    def send_single_advance(self):
        if not self.ser or not self.ser.is_open: return
        dist = self.cb_distance.get()
        cmds = [f"M611 N{self.port_n} S1", f"M600 N{self.port_n} F{dist} X1", f"M611 N{self.port_n} S0"]
        self.write_sys_log(f"Command: Single Advance (Port-N={self.port_n}, Dist={dist})")
        threading.Thread(target=self._execute_sequence, args=(cmds, 200), daemon=True).start()

    def send_activate_command(self):
        if not self.ser or not self.ser.is_open: return
        cmds = ["M610 S1", "M611 S0", f"M611 N{self.port_n} S1"]
        self.write_sys_log(f"Command: Activate Port-N={self.port_n}")
        threading.Thread(target=self._execute_sequence, args=(cmds, 300), daemon=True).start()

    def send_deactivate_command(self):
        if not self.ser or not self.ser.is_open: return
        cmds = ["M611 S0"]
        self.write_sys_log(f"Command: Deactivate All (M611 S0)")
        threading.Thread(target=self._execute_sequence, args=(cmds, 200), daemon=True).start()

    def toggle_repeat_advance(self):
        if not self.ser or not self.ser.is_open: return
        if not self.is_repeating:
            self.is_repeating = True
            self.btn_repeat_adv.config(bg=self.clr_btn_repeat_active)
            for btn in self.action_btns:
                btn.config(state=tk.DISABLED, bg=self.clr_disabled_bg, fg=self.clr_disabled_fg)
            threading.Thread(target=self._repeat_advance_loop, daemon=True).start()
        else:
            self.is_repeating = False
            self.btn_repeat_adv.config(bg=self.clr_btn_repeat)
            for btn in self.action_btns:
                orig_clr = self.clr_btn_default 
                if btn == self.btn_set_angle: orig_clr = self.clr_btn_angle
                elif btn == self.btn_single_adv: orig_clr = self.clr_btn_single
                elif btn == self.btn_activate: orig_clr = self.clr_btn_activate
                elif btn == self.btn_get_all: orig_clr = self.clr_btn_get
                elif btn == self.btn_update_n: orig_clr = self.clr_btn_update
                elif btn == self.btn_bfc_calibrate: orig_clr = self.clr_btn_calibration
                elif btn == self.btn_send: orig_clr = self.clr_send_default
                fg = "black" if btn == self.btn_activate else "white"
                btn.config(state=tk.NORMAL, bg=orig_clr, fg=fg)

    def _repeat_advance_loop(self):
        cmds_act = ["M610 S1", "M611 S0", f"M611 N{self.port_n} S1"]
        self._execute_sequence(cmds_act, 300)
        dist = self.cb_distance.get()
        single_cmds = [f"M611 N{self.port_n} S1", f"M600 N{self.port_n} F{dist} X1", f"M611 N{self.port_n} S0"]
        while self.is_repeating:
            self.write_sys_log(f"Loop: Sending Single Advance for Port-N={self.port_n}")
            self._execute_sequence(single_cmds, 200)
            try:
                delay = max(1200, int(self.ent_time.get()))
                self.ent_time.delete(0, tk.END)
                self.ent_time.insert(0, str(delay))


            except: delay = 1200
            start_wait = time.time()
            while self.is_repeating and (time.time() - start_wait) < (delay / 1000.0):
                time.sleep(0.1)

    def send_get_settings(self):
        if not self.ser or not self.ser.is_open: return
        board = self.var_board_addr.get()
        cmd = f"M621 B{board}"
        self.write_sys_log(f"Command: Get All Settings for Board={board}")
        threading.Thread(target=self._execute_sequence, args=([cmd], 200), daemon=True).start()

    def send_update_command(self):
        if not self.ser or not self.ser.is_open: return
        p = {k: v.get() for k, v in self.param_entries.items()}
        cmd = f"M620 N{self.port_n} A{p['A']} B{p['B']} C{p['C']} F{p['F']} U{p['U']} V{p['V']} W{p['W']} X{p['X']}"
        self.write_sys_log(f"Command: Update Port-N={self.port_n} Settings")
        threading.Thread(target=self._execute_sequence, args=([cmd], 200), daemon=True).start()

    def start_bfc_calibration(self):
        if not self.ser or not self.ser.is_open or self.is_calibrating:
            return

        loop_count = int(self.var_controller_count.get())
        forced_reset = bool(self.var_forced_calibration.get())
        if self.is_repeating:
            self.toggle_repeat_advance()

        if loop_count <= 0 and not forced_reset:
            self.write_sys_log("BFC calibration skipped: loop count is 0.")
            return

        self.set_calibration_state(True)
        threading.Thread(target=self._run_bfc_calibration, args=(loop_count, forced_reset), daemon=True).start()

    def _run_bfc_calibration(self, loop_count, forced_reset):
        self.rx_paused = True
        time.sleep(0.05)
        self.rx_buffer = ""
        self.write_sys_log(
            f"BFC calibration requested with loop count={loop_count}, forced={forced_reset}."
        )

        try:
            completed_loops = perform_bfc_calibration(
                self.ser,
                loop_count=loop_count,
                forced_reset=forced_reset,
                log_system=self.write_sys_log,
                log_incoming=lambda text: self.update_log_ui(text, "incoming"),
                log_outgoing=lambda text: self.update_log_ui(text, "outgoing"),
            )
            self.write_sys_log(f"BFC calibration finished: {completed_loops}/{loop_count} loop(s) completed.")
        except Exception as e:
            self.write_sys_log(f"BFC calibration failed: {e}")
        finally:
            self.rx_buffer = ""
            self.rx_paused = False
            self.root.after(0, self.set_calibration_state, False)

    def toggle_serial(self):
        if self.ser and self.ser.is_open:
            if self.is_repeating: self.toggle_repeat_advance() 
            self.ser.write(b"M611 S0\n")
            self.ser.flush()
            self.is_receiving = False
            time.sleep(0.3)
            
            self.ser.close()
            self.btn_connect.config(text="Connect", bg=self.clr_accent)
            self.update_button_states(False)
            self.write_sys_log("--- Connection Closed ---")
        else:
            try:
                port, baud = self.port_combo.get(), self.baud_combo.get()
                if not port: return
                self.ser = serial.Serial(port, baud, timeout=5)
                self.is_receiving = True
                self.btn_connect.config(text="Disconnect", bg=self.clr_success)
                self.update_button_states(True)
                self.write_sys_log(f"--- Connected to {port} ---")
                threading.Thread(target=self.rx_thread, daemon=True).start()
                self.root.after(1000, lambda: threading.Thread(target=self._execute_sequence, args=(["M115", "M610 S1", "M611 S0"], 300), daemon=True).start())
            except Exception as e: self.write_sys_log(f"Connection Failed: {e}")

    def rx_thread(self):
        while self.is_receiving:
            if self.ser and self.ser.is_open:
                try:
                    if self.rx_paused:
                        time.sleep(0.02)
                        continue
                    if self.ser.in_waiting > 0:
                        data = self.ser.read(self.ser.in_waiting).decode('utf-8', errors='replace')
                        self.rx_buffer += data
                        while "\n" in self.rx_buffer:
                            line, self.rx_buffer = self.rx_buffer.split("\n", 1)
                            if line.strip(): self.update_log_ui(line.strip(), "incoming")
                    time.sleep(0.02)
                except: break
            else: time.sleep(0.1)

    def send_data(self):
        if not self.ser or not self.ser.is_open: return
        content = self.input_text.get("1.0", tk.END).strip()
        if content:
            try:
                self.ser.write((content + "\n").encode('utf-8'))
                self.ser.flush()
                self.update_log_ui(content, "outgoing")
            except Exception as e: self.write_sys_log(f"Error: {e}")

    def _execute_sequence(self, commands, delay_ms=200):
        for cmd in commands:
            if not self.ser or not self.ser.is_open: break
            try:
                self.ser.write((cmd.strip() + "\n").encode('utf-8'))
                self.ser.flush()
                self.root.after(0, self.update_log_ui, cmd, "outgoing")
                time.sleep(delay_ms / 1000.0)
            except: break

    def update_log_ui(self, text, direction):
        def _update():
            self.log_display.config(state=tk.NORMAL)
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            if direction == "incoming":
                self.log_display.insert(tk.END, f"{ts} »\n", "incoming_ts")
                self.log_display.insert(tk.END, f"{text}\n\n")
            else:
                self.log_display.insert(tk.END, f"« {ts}\n", "outgoing_ts")
                self.log_display.insert(tk.END, f"{text}\n\n", "outgoing_msg")
            self.log_display.see(tk.END)
            self.log_display.config(state=tk.DISABLED)
        self.root.after(0, _update)

    def write_sys_log(self, info):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.write_sys_log, info)
            return
        self.log_display.config(state=tk.NORMAL)
        self.log_display.insert(tk.END, f"{info}\n", "sys_info")
        self.log_display.see(tk.END)
        self.log_display.config(state=tk.DISABLED)

    def on_closing(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(b"M611 S0\n")
                self.ser.flush(); time.sleep(0.3); self.ser.close()
            except: pass
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = SerialTool(root)
    root.minsize(1024, 720)
    root.mainloop()
