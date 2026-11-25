# Avisos meteorológicos de SENAMHI (precipitación)

Aquí van los zip que se descargan desde la página de cada aviso en SENAMHI
(botón de descarga del shapefile). Alimentan la pestaña **Avisos precipitación** del dashboard.

- **Para agregar un aviso:** *Add file → Upload files*, arrastra los zip de todos sus días
  (un zip por mapa/día, p. ej. `shp_aviso_481_1_2026.zip`, `shp_aviso_481_2_2026.zip`, `shp_aviso_481_3_2026.zip`)
  y haz *Commit changes*. El dashboard se actualiza solo en 1–2 minutos.
- **No descomprimas los zip:** se leen tal cual.
- **El nombre del archivo no importa:** el número de aviso, el día y los niveles se leen del propio shapefile.
  Si un mismo mapa se sube dos veces, se muestra una sola vez.
- **No borres los avisos antiguos:** quedan como historial en "Anteriores".
- **Título y descripción (opcional):** el shapefile no los trae. Si quieres verlos en el dashboard,
  agrega una fila en `avisos.csv` (columnas `aviso,anio,titulo,descripcion`). Si el texto tiene comas, va entre comillas.
