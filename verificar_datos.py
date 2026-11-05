"""Verifica, con las APIs reales, qué serie se usa para cada indicador y si los datos están al día.

Uso (en la Terminal, dentro de la carpeta del proyecto):
    python3 verificar_datos.py

Si aparece un error de certificado SSL del BCRA, probá:
    BCRA_SSL_VERIFY=0 python3 verificar_datos.py

Genera 'verificacion_fuentes.csv' y muestra un resumen en pantalla. Pegá ese resumen en el chat.
"""
import pandas as pd

import datos

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 30)
pd.set_option("display.max_colwidth", 70)


def main():
    print("Consultando las APIs oficiales (puede tardar un minuto)...\n")
    tabla = datos.verificar_fuentes()
    tabla.to_csv("verificacion_fuentes.csv", index=False)

    cols = [c for c in ["Indicador", "Estado", "Serie usada", "Unidad", "Frecuencia", "Último dato", "Atraso (días)"]
            if c in tabla.columns]
    print("=== SERIE USADA PARA CADA INDICADOR ===")
    print(tabla[cols].to_string(index=False))

    problemas = tabla[tabla["Estado"] != "OK"]
    if len(problemas):
        print("\n=== INDICADORES A REVISAR (con el detalle de lo que se descartó) ===")
        for _, f in problemas.iterrows():
            print(f"\n- {f['Indicador']}  [{f['Estado']}]")
            print(f"  Título oficial: {f.get('Título oficial', '')}")
            print(f"  Notas: {f.get('Notas', '')}")

    print("\n=== CHEQUEO DE INFLACION (dic/dic) CONTRA CIFRAS DEL INDEC ===")
    try:
        chequeo = datos.chequeos_inflacion()
        print(chequeo.to_string(index=False))
        print("\nRESULTADO:", "todas coinciden" if chequeo["Coincide"].all() else "HAY DIFERENCIAS: revisar la serie del IPC")
    except Exception as e:
        print("No se pudo hacer el chequeo:", e)

    print(f"\nTotal: {(tabla['Estado'] == 'OK').sum()} OK, {len(problemas)} para revisar.")
    print("Se guardó el detalle completo en verificacion_fuentes.csv")


if __name__ == "__main__":
    main()
