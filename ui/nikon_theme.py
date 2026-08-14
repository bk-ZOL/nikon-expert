"""
Nikon Expert — 自定义 Gradio 主题(Claude 风格)
================================================
学 Claude 界面:暖纸感底、一栏居中、正文衬线、大量留白、陶土色只做点缀。
错误码/图号仍用等宽字体(贴光刻设备的使用场景)。

接进你现有的 app:
    from nikon_theme import nikon_theme
    with gr.Blocks(theme=nikon_theme, css_paths="nikon_expert.css") as demo:
        ...
"""

import gradio as gr
from gradio.themes.utils import sizes

# ── 陶土色(Claude 的 clay accent)—— 只做点缀 ────────────────
clay = gr.themes.Color(
    c50="#FBF0EB", c100="#F5DED3", c200="#EBC0AD", c300="#DFA184",
    c400="#D4896A", c500="#CC785C", c600="#B8634A", c700="#9A4F3B",
    c800="#7B3F30", c900="#5F3226", c950="#3A1E17", name="clay",
)

# ── 暖中性色(纸感)───────────────────────────────────────────
stone = gr.themes.Color(
    c50="#FAF9F5", c100="#F0EEE6", c200="#E8E5DC", c300="#D6D2C4",
    c400="#B0AB9C", c500="#84806F", c600="#6E6A5A", c700="#514E43",
    c800="#35332C", c900="#262624", c950="#1A1A18", name="warm-stone",
)


class NikonExpert(gr.themes.Base):
    def __init__(self):
        super().__init__(
            primary_hue=clay,
            secondary_hue=clay,
            neutral_hue=stone,
            spacing_size=sizes.spacing_lg,     # 更松,更多留白
            radius_size=sizes.radius_md,
            text_size=sizes.text_md,
            font=[gr.themes.GoogleFont("Inter"), "system-ui", "-apple-system", "sans-serif"],
            font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
        )
        super().set(
            # —— 暖纸感底 ——
            body_background_fill="#FAF9F5",
            body_background_fill_dark="#262624",
            background_fill_primary="#FFFFFF",
            background_fill_primary_dark="#30302E",
            background_fill_secondary="#F0EEE6",
            background_fill_secondary_dark="#262624",

            # —— 文字:暖近黑 ——
            body_text_color="#2B2B26",
            body_text_color_dark="#F0EEE6",
            body_text_color_subdued="#84806F",
            body_text_color_subdued_dark="#B0AB9C",
            link_text_color="*primary_600",
            link_text_color_dark="*primary_300",
            link_text_color_hover="*primary_700",

            # —— 卡片/区块:几乎无边框,靠留白 ——
            block_background_fill="transparent",
            block_background_fill_dark="transparent",
            block_border_width="0px",
            block_border_color="#E8E5DC",
            block_border_color_dark="#3D3D38",
            block_radius="*radius_lg",
            block_shadow="none",
            block_label_background_fill="transparent",
            block_label_text_color="#84806F",
            block_label_text_color_dark="#B0AB9C",
            block_label_text_weight="500",
            block_title_text_weight="600",
            panel_background_fill="transparent",
            panel_border_width="0px",
            border_color_primary="#E8E5DC",
            border_color_primary_dark="#3D3D38",

            # —— 输入框:大圆角、软阴影,像 Claude 的输入条 ——
            input_background_fill="#FFFFFF",
            input_background_fill_dark="#30302E",
            input_border_color="#E8E5DC",
            input_border_color_dark="#3D3D38",
            input_border_color_focus="#D6D2C4",
            input_border_color_focus_dark="#514E43",
            input_border_width="1px",
            input_shadow="0 1px 2px rgba(43,43,38,.04)",
            input_shadow_focus="0 2px 10px rgba(43,43,38,.06)",

            # —— 主按钮:陶土色 ——
            button_large_radius="*radius_lg",
            button_small_radius="*radius_lg",
            button_primary_background_fill="*primary_500",
            button_primary_background_fill_hover="*primary_600",
            button_primary_background_fill_dark="*primary_500",
            button_primary_background_fill_hover_dark="*primary_400",
            button_primary_text_color="#FFFFFF",
            button_primary_border_color="*primary_500",
            button_primary_border_color_dark="*primary_500",

            # —— 次按钮:极低调 ——
            button_secondary_background_fill="transparent",
            button_secondary_background_fill_hover="#F0EEE6",
            button_secondary_background_fill_dark="transparent",
            button_secondary_background_fill_hover_dark="#35332C",
            button_secondary_text_color="#514E43",
            button_secondary_text_color_dark="#D6D2C4",
            button_secondary_border_color="#E8E5DC",
            button_secondary_border_color_dark="#3D3D38",

            color_accent="*primary_500",
            color_accent_soft="*primary_50",
            color_accent_soft_dark="#35332C",
        )


nikon_theme = NikonExpert()
