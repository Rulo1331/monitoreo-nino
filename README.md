# Comunicados oficiales del ENFEN

Aquí van los PDF de los comunicados oficiales del ENFEN, descargados de https://enfen.imarpe.gob.pe/

- **Para agregar uno:** *Add file → Upload files*, arrastra el PDF y haz *Commit changes*.
  El workflow **ENFEN comunicados** se ejecuta solo y actualiza la tabla `enfen_comunicados` en BigQuery.
- **Nombre del archivo:** puede ser cualquiera. El número se lee del propio PDF ("N° 20-2026").
  Si no aparece, se toma del nombre, así que conviene que incluya el número: `Comunicado-20-2026.pdf`.
- **No borres los PDF antiguos:** el recolector se detiene si la carpeta tiene menos comunicados que la tabla.
- **Probabilidades:** vienen en una figura del comunicado; se cargan a mano en `datos/enfen_probabilidades.csv`.
- **Si un dato quedó mal leído** (columna `revisar = true` en la tabla), se corrige en `datos/enfen_correcciones.csv`.
