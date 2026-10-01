# -*- coding: utf-8 -*-
"""role_image.py — 角色列表图片绘制（Pillow；缓存签名判断在 main 侧）"""

import os

from astrbot.api import logger

def build(vm, out_path: str, category=None):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.error("[语音罐头] 生成角色列表图片需要 Pillow 库，请安装: pip install Pillow")
        return None

    # 按拼音首字母排序
    try:
        from pypinyin import lazy_pinyin
        roles = sorted(role_names, key=lambda h: lazy_pinyin(h))
    except ImportError:
        import locale
        try:
            roles = sorted(role_names, key=locale.strxfrm)
        except Exception:
            roles = sorted(role_names)
    if not roles:
        return None

    # ---- 布局参数 ----
    cols = 8
    rows_count = -(-len(roles) // cols)  # 向上取整

    card_w = 128
    card_h = 42
    gap_x = 8
    gap_y = 6
    pad_x = 48
    pad_top = 110
    pad_bottom = 50

    img_w = pad_x * 2 + cols * card_w + (cols - 1) * gap_x
    img_h = pad_top + rows_count * (card_h + gap_y) + pad_bottom

    # ---- 创建画布 ----
    img = Image.new('RGB', (img_w, img_h), '#121214')
    draw = ImageDraw.Draw(img)

    # ---- 加载字体 ----
    def _try_fonts(names, size):
        for name in names:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        return ImageFont.load_default()

    font_title = _try_fonts(['msyh.ttc', 'msyhbd.ttc', 'simhei.ttf', 'simsun.ttc'], 36)
    font_sub = _try_fonts(['msyh.ttc', 'simhei.ttf'], 16)
    font_name = _try_fonts(['msyh.ttc', 'simhei.ttf', 'simsun.ttc'], 17)
    font_count = _try_fonts(['msyh.ttc', 'simhei.ttf'], 11)

    # ---- 绘制标题区域 ----
    if category:
        title = f"{category} · 角色列表"
    else:
        title = "语音库 · 角色列表"
    draw.text((img_w // 2, 35), title, fill='#FFFFFF', font=font_title, anchor='mt')

    # 装饰线
    line_w = 200
    lx = (img_w - line_w) // 2
    draw.line([(lx, 72), (lx + line_w, 72)], fill='#3A3A42', width=2)
    # 装饰线两端点缀
    draw.ellipse([(lx - 3, 69), (lx + 3, 75)], fill='#E62828')
    draw.ellipse([(lx + line_w - 3, 69), (lx + line_w + 3, 75)], fill='#E62828')

    if category:
        subtitle = f"共收录 {role_count} 位角色"
    else:
        cat_count = len(vm.category_order)
        subtitle = f"共收录 {role_count} 位角色" + (f" · {cat_count} 个语音库" if cat_count > 1 else "")
    draw.text((img_w // 2, 85), subtitle, fill='#9999A2', font=font_sub, anchor='mt')

    # ---- 绘制角色卡片网格 ----
    for i, role in enumerate(roles):
        col = i % cols
        row = i // cols

        x = pad_x + col * (card_w + gap_x)
        y = pad_top + row * (card_h + gap_y)

        # 卡片背景
        draw.rounded_rectangle(
            [(x, y), (x + card_w, y + card_h)],
            radius=6, fill='#1A1A1E', outline='#2A2A30'
        )
        # 顶部微高光
        draw.line(
            [(x + 8, y + 1), (x + card_w - 8, y + 1)],
            fill='#2E2E36', width=1
        )

        # 角色名（居中，超长名称自动截断；指定库时不带库前缀，汇总时带）
        if category:
            voice_count = len(vm.categories[category][role])
            display_name = role
        else:
            voice_count = len(vm.role_map[role])
            display_name = vm.role_display(role)
        bbox = draw.textbbox((0, 0), display_name, font=font_name)
        text_w = bbox[2] - bbox[0]
        truncated = False
        while text_w > card_w - 12 and len(display_name) > 1:
            display_name = display_name[:-1]
            truncated = True
            bbox = draw.textbbox((0, 0), display_name + '…', font=font_name)
            text_w = bbox[2] - bbox[0]
        if truncated:
            display_name += '…'
        draw.text(
            (x + card_w // 2, y + card_h // 2 - 3),
            display_name, fill='#FFFFFF', font=font_name, anchor='mm'
        )

        # 语音条数标记
        count_text = f"{voice_count}条"
        draw.text(
            (x + card_w // 2, y + card_h - 7),
            count_text, fill='#666670', font=font_count, anchor='mm'
        )

    # ---- 底部信息 ----
    footer = "发送「角色名0」听全部语音  ·  发送「随机台词N」随机N条"
    draw.text((img_w // 2, img_h - 20), footer, fill='#555560', font=font_count, anchor='mm')

    # ---- 保存 ----
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, quality=95)
    logger.info(f"[语音罐头] 角色列表图片已生成: {out_path}")
    return out_path
