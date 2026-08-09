import sys
import random
import math
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QPainter, QColor, QFont, QFontMetrics, QPen, QPainterPath, QTransform

LINES = [
    "我直接一个闪现接金身然后白给",
    "队友呢队友呢救一下啊",
    "这波操作下饭得我连干三碗米饭",
    "你的素养很差不如我的一键喊话",
    "别吵别吵这里关键团",
    "感觉你这个人机有点过于智能了",
    "破防了家人们谁懂啊",
    "从未如此美妙的开局双手离开键盘",
    "你是来拉屎的吧",
    "急了急了有人急了",
    "典中典之典中典",
    "这游戏蒸馍玩啊",
    "我起了被秒了有什么好说的",
    "不会玩能不能别选这个啊",
    "我的我的我的我的",
    "哈基米哈基米胖宝宝",
    "兄弟你什么段位啊",
    "人不行别怪路不平",
    "完了全完了",
    "原神启动",
]

class FadingLine:
    def __init__(self, text, font, x, y, angle, color, stroke_color, spacing, persp_transform):
        self.text = text
        self.font = font
        self.x = x
        self.y = y
        self.angle = angle
        self.color = QColor(color)
        self.stroke_color = QColor(stroke_color)
        self.spacing = spacing
        self.persp_transform = persp_transform
        self.alpha = 255
        self.fade_speed = 8
        self.rise_speed = 1.5

    def update(self):
        self.alpha = max(0, self.alpha - self.fade_speed)
        self.y -= self.rise_speed
        return self.alpha > 0

    def draw(self, painter):
        if self.alpha <= 0:
            return
        painter.save()
        # 淡出也用同样的透视
        painter.setTransform(self.persp_transform, True)
        painter.translate(self.x, self.y)
        font = QFont(self.font)
        fm = QFontMetrics(font)
        th = fm.height()
        angle_rad = math.radians(self.angle)
        shadow_c = QColor(self.stroke_color)
        shadow_c.setAlpha(self.alpha)
        text_c = QColor(self.color)
        text_c.setAlpha(self.alpha)

        cursor = 0
        for ch in self.text:
            cw = fm.horizontalAdvance(ch)
            ox = cursor * math.cos(angle_rad)
            oy = cursor * math.sin(angle_rad)

            path_shadow = QPainterPath()
            path_shadow.addText(ox + 3, oy + 3 + th/3, font, ch)
            painter.setPen(Qt.NoPen)
            painter.setBrush(shadow_c)
            painter.drawPath(path_shadow)

            path_text = QPainterPath()
            path_text.addText(ox, oy + th/3, font, ch)
            painter.setPen(Qt.NoPen)
            painter.setBrush(text_c)
            painter.drawPath(path_text)

            cursor += cw + self.spacing
        painter.restore()


class TestWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self.screen_w = screen.width()
        self.screen_h = screen.height()

        self.text_color = QColor("#fffeef")
        self.stroke_color = QColor("#d8a523")
        self.font = QFont("Microsoft YaHei", 28, QFont.Bold)
        self.spacing = 5
        self.shake_intensity = 2
        self.perspective_enabled = True
        self.perspective_strength = 1.0

        self.current_text = ""
        self.char_index = 0
        self.x = 500
        self.y = 300
        self.angle = random.randint(-10, 10)
        self.char_shakes = []
        self.fading_lines = []

        self.persp_transform = QTransform()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.show_next)
        self.shake_timer = QTimer(self)
        self.shake_timer.timeout.connect(self.update_shake)
        self.fade_timer = QTimer(self)
        self.fade_timer.timeout.connect(self.update_fading)
        self.fade_timer.start(30)

        self.next_sentence()

    def compute_perspective(self):
        if not self.perspective_enabled:
            self.persp_transform = QTransform()
            return
        rel_x = (self.x - self.screen_w / 2) / (self.screen_w / 2)
        rel_y = (self.y - self.screen_h / 2) / (self.screen_h / 2)
        persp_x = 0.00005 * self.perspective_strength * rel_x
        persp_y = 0.0003 * self.perspective_strength * rel_y
        
        # 水平补偿：右边稍微放大
        scale_x = 1.0 + 0.03 * self.perspective_strength * max(0, rel_x)
        
        self.persp_transform = QTransform()
        self.persp_transform.setMatrix(scale_x, 0, persp_x,
                                       0, 1, persp_y,
                                       0, 0, 1)

    def next_sentence(self):
        if self.current_text and self.char_index > 0:
            fading = FadingLine(
                self.current_text[:self.char_index], self.font,
                self.x, self.y, self.angle,
                self.text_color, self.stroke_color, self.spacing,
                self.persp_transform
            )
            self.fading_lines.append(fading)

        line = random.choice(LINES)
        self.current_text = line
        self.char_index = 0
        self.char_shakes = [{'x': 0, 'y': 0, 'target_x': 0, 'target_y': 0} for _ in line]
        self.x = random.randint(300, self.screen_w - 300)
        self.y = random.randint(200, self.screen_h - 200)
        self.angle = random.randint(-10, 10)
        self.compute_perspective()
        self.timer.start(100)
        self.shake_timer.start(143)

    def show_next(self):
        if self.char_index < len(self.current_text):
            self.char_index += 1
            self.update()
        else:
            self.timer.stop()
            QTimer.singleShot(2000, self.next_sentence)

    def update_shake(self):
        for s in self.char_shakes:
            s['target_x'] = random.randint(-self.shake_intensity, self.shake_intensity)
            s['target_y'] = random.randint(-self.shake_intensity, self.shake_intensity)
            s['x'] += (s['target_x'] - s['x']) * 0.3
            s['y'] += (s['target_y'] - s['y']) * 0.3
        self.update()

    def update_fading(self):
        if not self.fading_lines:
            return
        self.fading_lines = [f for f in self.fading_lines if f.update()]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        for f in self.fading_lines:
            f.draw(painter)

        draw_text = self.current_text[:self.char_index]
        if not draw_text:
            return

        font = QFont(self.font)
        fm = QFontMetrics(font)
        th = fm.height()
        angle_rad = math.radians(self.angle)

        painter.save()
        if self.perspective_enabled:
            painter.setTransform(self.persp_transform, True)
        painter.translate(self.x, self.y)

        cursor = 0
        for i, ch in enumerate(draw_text):
            sx = self.char_shakes[i]['x'] if i < len(self.char_shakes) else 0
            sy = self.char_shakes[i]['y'] if i < len(self.char_shakes) else 0
            cw = fm.horizontalAdvance(ch)
            ox = cursor * math.cos(angle_rad)
            oy = cursor * math.sin(angle_rad)

            path_shadow = QPainterPath()
            path_shadow.addText(ox + sx + 3, oy + sy + 3 + th/3, font, ch)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.stroke_color)
            painter.drawPath(path_shadow)

            path_text = QPainterPath()
            path_text.addText(ox + sx, oy + sy + th/3, font, ch)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.text_color)
            painter.drawPath(path_text)

            cursor += cw + self.spacing

        painter.restore()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            QApplication.quit()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = TestWindow()
    w.show()
    app.exec_()