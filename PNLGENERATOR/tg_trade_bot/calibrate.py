from PIL import Image
import matplotlib.pyplot as plt

img = Image.open("template.png")  # твой шаблон
w, h = img.size

print("Размер:", w, h)


def onclick(event):
    if event.xdata and event.ydata:
        x = event.xdata / w
        y = event.ydata / h

        print(f"x: {x:.4f}, y: {y:.4f}")


plt.imshow(img)
plt.title("Кликни по нужному месту")
plt.axis("off")

plt.connect("button_press_event", onclick)
plt.show()
