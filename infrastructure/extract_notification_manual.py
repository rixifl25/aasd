from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import logging
import os
import time
# from PyPDF2 import PdfReader
from io import BytesIO
import requests

from application.http_session_rpa import HttpSessionRpa
from application.extract_notification_base import ExtractNotificationBase

from google.cloud import storage
import logging
from dateutil import parser

logger = logging.getLogger(__name__)

class ExtractNotificationManual(ExtractNotificationBase):
    def __init__(self, config):
        self.config = config

    # @staticmethod
    def upload_to_gcs(self, pdf_content: bytes, destination_path: str):
        bucket_name = "notificaciones-sunat-store"

        try:
            client = storage.Client(project="estudios-contables")
            bucket = client.bucket(bucket_name)

            blob = bucket.blob(destination_path)
            blob.upload_from_file(pdf_content, content_type="application/pdf")

            logger.info(f"Archivo subido a gs://{bucket_name}/{destination_path}")

        except Exception as e:
            logger.error(f"Error al subir archivo a GCS: {e}")

    def download_attachment(self, page_source, cookies_dict, notification_type, context):
        soup_detail = BeautifulSoup(page_source, 'html.parser')

        download_links = soup_detail.find_all('a', href=lambda x: x and "goArchivoDescarga" in x)
        gcs_paths = []

        for download_link in download_links:
            onclick_text = download_link['href']
            params = onclick_text.split('goArchivoDescarga(')[1].split(')')[0].split(',')
            id_archivo, ind_mensaje, id_mensaje = [p.strip() for p in params]

            logger.info(f"Descargando archivo - ID Mensaje: {id_mensaje}, ID Archivo: {id_archivo}, Ind Mensaje: {ind_mensaje}")

            data = {
                "accion": "archivo",
                "idMensaje": id_mensaje,
                "idArchivo": id_archivo,
                "sistema": ind_mensaje,
                "indMensaje": "5"
            }
            url = self.config["WEBSITE"]["url_download_attach"]
            response = requests.post(url, data=data, cookies=cookies_dict)

            if response.status_code == 200:
                content_type = response.headers.get('Content-Type')
                content_disposition = response.headers.get('Content-Disposition', '')

                filename = "{}_{}_{}".format(id_mensaje, id_archivo, ind_mensaje)
                if "filename=" in content_disposition:
                    filename += "_" + content_disposition.split("filename=")[-1].strip().replace('"', '')

                # file_path = os.path.join("descargas", filename)
                # os.makedirs("descargas", exist_ok=True)
                # with open(file_path, "wb") as f:
                #     f.write(response.content)
                # logger.info(f"Archivo guardado: {file_path}")

                if "application/pdf" in content_type:
                    try:
                        pdf_bytes = BytesIO(response.content)
                        gcs_path = f"{context['estudio_contable_ruc']}/{context['ruc']}/{notification_type}/{filename}".replace(" ", "_")
                        self.upload_to_gcs(pdf_bytes, gcs_path)
                        gcs_paths.append("gs://notificaciones-sunat-store/" + gcs_path)
                        # pdf_text = "".join(page.extract_text() or "" for page in reader.pages)
                        # logger.info(f"Texto extraído del PDF:{pdf_text[:1000]}")
                    except Exception as e:
                        gcs_path = None
                        logger.warning(f"Error al subir el PDF a GCS: {e}")
                else:
                    logger.info(f"Archivo descargado, pero no es un PDF. Tipo de contenido: {content_type}")
            else:
                logger.warning(f"No se pudo descargar el archivo {id_archivo}, status: {response.status_code}")

        return gcs_paths

    def extract(self, session: HttpSessionRpa, context):
        # url = "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/bajarArchivo"
        notification_data = []

        try:
            WebDriverWait(session.automator.driver, 10).until(
                EC.frame_to_be_available_and_switch_to_it((By.NAME, "iframeApplication"))
            )

            lista = session.automator.driver.find_element(By.XPATH, '//ul[@id="listaMensajes"]')
            if not lista.text:
                return notification_data

            notification_elements = WebDriverWait(session.automator.driver, 10).until(
                EC.visibility_of_all_elements_located((By.XPATH, '//ul[@id="listaMensajes"]/li')))

            def is_recent_than(notification, last_date):
                if last_date is None:
                    return True
                    
                item = BeautifulSoup(notification.get_attribute("outerHTML"), 'html.parser')
                
                try:
                    # Parse both dates
                    valid_last_date = parser.parse(last_date)
                    item_date = parser.parse(item.find('small', class_="text-muted fecPublica").text, parser.parserinfo(dayfirst=True))
                    
                    # Make both timezone-aware or both timezone-naive
                    if valid_last_date.tzinfo is not None and item_date.tzinfo is None:
                        # If last_date has timezone but item_date doesn't, make item_date timezone-aware
                        item_date = item_date.replace(tzinfo=valid_last_date.tzinfo)
                    elif valid_last_date.tzinfo is None and item_date.tzinfo is not None:
                        # If item_date has timezone but last_date doesn't, make last_date timezone-aware
                        valid_last_date = valid_last_date.replace(tzinfo=item_date.tzinfo)
                    
                    return item_date >= valid_last_date
                except Exception as e:
                    logger.warning(f"Error comparing dates: {e}")
                    return True  # Default to including the notification if there's an error
                
            new_elements = [n for n in notification_elements
                        if is_recent_than(n, context["last_date"])
                    ]
            logger.info(f"Nuevas Notificaciones: {len(new_elements)}")
            logger.info(f"Última Notificación: {context['last_date']}")

            for notification in new_elements:
                # logger.debug(notification.get_attribute("outerHTML"))
                soup = BeautifulSoup(notification.get_attribute("outerHTML"), 'html.parser')

                subject = soup.find('a', class_="linkMensaje text-muted").text
                if subject.upper().startswith("ASUNTO: "):
                    subject = subject[8:]

                publish_date = soup.find('small', class_="text-muted fecPublica").text
                notification_id = notification.get_property('id') 
                notification_type = "SIN TIPO"
                if len(soup.select('div>span[class*="label tag"]')) > 0:
                    notification_type = soup.select('div>span[class*="label tag"]')[0].text

                try:
                    link_element = notification.find_element(By.CLASS_NAME, "linkMensaje")
                    link_element.click()
                except Exception as e:
                    logger.warning(f"Elemento no encontrado: {e}")
                    continue

                try:
                    session.automator.driver.switch_to.frame(session.automator.driver.find_element(By.NAME, "contenedorMensaje"))
                    cookies_dict = {cookie['name']: cookie['value'] for cookie in session.automator.driver.get_cookies()}

                    page_source = session.automator.driver.page_source
                    gcs_paths = self.download_attachment(page_source, cookies_dict, notification_type, context)
                except Exception as e:
                    logger.warning(f"No se pudo acceder al iframe del mensaje: {e}")

                notification_info = {
                    "id": notification_id,
                    "subject": subject,
                    "publish_date": publish_date,
                    "type": notification_type,
                    "url_archivo": ",".join(gcs_paths)
                }
                notification_data.append(notification_info)
                
                session.automator.driver.switch_to.default_content()
                WebDriverWait(session.automator.driver, 10).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "iframeApplication")))

        except TimeoutError as e:
            logger.error(f"Error inesperado en el proceso de extracción: {e}")
        # except Notfound
        except Exception as e:
            logger.error(f"Error inesperado en el proceso de extracción: {e}")
        finally:
            # session.automator.driver.quit()
            session.automator.driver.switch_to.default_content()
        return notification_data
