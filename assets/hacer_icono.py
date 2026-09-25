"""
Genera assets/icono.ico (y una vista previa en PNG) para el ejecutable.

Idea: una onda de sonido que se convierte en líneas de texto (audio -> texto)
sobre un cuadrado redondeado con degradado violeta -> azul. Los tamaños
pequeños (16-32 px) llevan un dibujo simplificado, con trazos más gruesos,
para que no se emborrone en la barra de tareas.

    .venv\\Scripts\\python.exe -m pip install pillow
    .venv\\Scripts\\python.exe assets\\hacer_icono.py
"""

import os

from PIL import Image, ImageDraw, ImageFilter

AQUI = os.path.dirname(os.path.abspath(__file__))
LIENZO = 1024  # se dibuja grande y se reduce: bordes suaves
TAMANOS = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

ARRIBA = (124, 58, 237)    # violeta
ABAJO = (37, 99, 235)      # azul
BLANCO = (255, 255, 255, 255)
TENUE = (255, 255, 255, 170)


def fondo(margen):
    """Cuadrado redondeado con degradado en diagonal."""
    lado = LIENZO - 2 * margen
    degradado = Image.new("RGBA", (LIENZO, LIENZO))
    px = degradado.load()
    for y in range(LIENZO):
        for x in range(LIENZO):
            t = (x + y) / (2 * (LIENZO - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(ARRIBA, ABAJO)) + (255,)
    mascara = Image.new("L", (LIENZO, LIENZO), 0)
    ImageDraw.Draw(mascara).rounded_rectangle(
        (margen, margen, margen + lado, margen + lado), radius=int(lado * 0.22), fill=255)
    img = Image.new("RGBA", (LIENZO, LIENZO), (0, 0, 0, 0))
    img.paste(degradado, (0, 0), mascara)

    # Brillo suave arriba: da volumen sin recargar
    brillo = Image.new("RGBA", (LIENZO, LIENZO), (0, 0, 0, 0))
    ImageDraw.Draw(brillo).ellipse((-LIENZO * 0.3, -LIENZO * 0.75, LIENZO * 1.3, LIENZO * 0.42),
                                   fill=(255, 255, 255, 40))
    brillo = brillo.filter(ImageFilter.GaussianBlur(LIENZO * 0.06))
    recorte = Image.new("RGBA", (LIENZO, LIENZO), (0, 0, 0, 0))
    recorte.paste(brillo, (0, 0), mascara)
    return Image.alpha_composite(img, recorte)


def barra(d, x, alto, ancho, color, centro=LIENZO / 2):
    d.rounded_rectangle((x - ancho / 2, centro - alto / 2, x + ancho / 2, centro + alto / 2),
                        radius=ancho / 2, fill=color)


def linea(d, x0, x1, y, grueso, color):
    d.rounded_rectangle((x0, y - grueso / 2, x1, y + grueso / 2), radius=grueso / 2, fill=color)


def icono_detallado():
    img = fondo(margen=48)
    # Se dibuja en una capa aparte y se funde: así lo semitransparente se
    # mezcla con el degradado en vez de dejar ver lo que hay detrás del icono
    capa = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    # Onda: barras de altura variable, la central la más alta
    for x, alto in zip((215, 300, 385, 470), (230, 470, 330, 180)):
        barra(d, x, alto, 58, BLANCO)
    # Flecha fina de paso
    d.polygon([(535, 455), (590, 512), (535, 569)], fill=TENUE)
    # Texto: líneas de distinta longitud, como un párrafo
    for y, x1, color in ((380, 840, BLANCO), (470, 800, BLANCO), (560, 840, BLANCO),
                         (650, 740, TENUE)):
        linea(d, 640, x1, y, 50, color)
    return Image.alpha_composite(img, capa)


def icono_simple():
    img = fondo(margen=24)
    d = ImageDraw.Draw(img)
    for x, alto in zip((250, 400), (340, 580)):
        barra(d, x, alto, 110, BLANCO)
    for y, x1 in ((380, 830), (512, 780), (644, 830)):
        linea(d, 540, x1, y, 96, BLANCO)
    return img


def main():
    detallado, simple = icono_detallado(), icono_simple()
    imagenes = [(simple if t <= 32 else detallado).resize((t, t), Image.LANCZOS) for t in TAMANOS]
    grande = imagenes[-1]
    grande.save(os.path.join(AQUI, "icono.ico"), format="ICO",
                sizes=[(t, t) for t in TAMANOS], append_images=imagenes[:-1])
    detallado.resize((512, 512), Image.LANCZOS).save(os.path.join(AQUI, "icono.png"))

    # Vista previa con todos los tamaños, sobre claro y sobre oscuro
    ancho = sum(TAMANOS) + 20 * (len(TAMANOS) + 1)
    muestra = Image.new("RGBA", (ancho, 2 * (256 + 40)), (245, 245, 245, 255))
    ImageDraw.Draw(muestra).rectangle((0, 296, ancho, 592), fill=(32, 32, 36, 255))
    x = 20
    for img in imagenes:
        for fila in (0, 296):
            muestra.alpha_composite(img, (x, fila + 20 + (256 - img.width) // 2))
        x += img.width + 20
    muestra.save(os.path.join(AQUI, "icono_muestra.png"))
    print("icono.ico, icono.png e icono_muestra.png generados en", AQUI)


if __name__ == "__main__":
    main()
