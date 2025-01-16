from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import requests
import time
from datetime import datetime
import logging

# Logging config
logging.basicConfig(
    filename="server.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class RequestHandler(BaseHTTPRequestHandler):

    # Get request configuration data
    with open('secrets.json', 'r') as file:
        secrets_json = json.load(file)
        
        # Refresh Request configuration (to obtain access_token)
        refresh_request_url = "https://oauth2.googleapis.com/token"
        refresh_request_headers = { "Content-Type": "application/x-www-form-urlencoded" }
        refresh_request_payload = {
            "client_id": secrets_json["client_id"],
            "client_secret": secrets_json["client_secret"],
            "refresh_token": secrets_json["refresh_token"],
            "grant_type": "refresh_token"
        }

        # Access Request configuration (to send FCM)
        fcm_url = secrets_json["fcm_url"]
        fcm_headers = { "Content-Type": "application/json", "Authorization": "Bearer <put access_token here>" }

    # Get users data
    users = {}
    with open('users.json', 'r') as file:
        users_json = json.load(file)
        for d in users_json["data"]:
            users[d["key"]] = [t for t in d["tokens"]]

    must_turn_alarm_off = {key:False for key in users} # true if the app ordered to turn the alarm off
    block_fcm = {key:False for key in users} # true if FCM messages are blocked


    '''
    Responds to request with an error
    '''
    def respond_error(self, code, log):
        logging.warning(f"Answered error {code}: {log}")
        self.send_response(code)
        self.end_headers()
    
    
    '''
    Sends a FCM message to each phone associated to the given key
    '''
    def send_fcm(self, key, title, message, timestamp, status):
        
        # Ask for access_token
        refresh_request_response = requests.post(self.refresh_request_url, data=self.refresh_request_payload, headers=self.refresh_request_headers)
        if refresh_request_response.status_code == 200:
            access_token = refresh_request_response.json()["access_token"]
            logging.info(f"FCM: fetched access_token.")
        else:
            logging.error(f"FCM: failed to fetch access_token from {self.refresh_request_url}.")
            logging.error(f"{refresh_request_response.status_code} : {refresh_request_response.text}")
            return
        
        # Update the access_token
        self.fcm_headers["Authorization"] = f"Bearer {access_token}"

        
        # HACK
        if self.block_fcm[key]:
            logging.info(f"FCM: simulated sending to {key}: {title} {message} {timestamp} {status}")
            return
        
        # Send FCM to each user of this key
        for token in self.users[key]:
            fcm_payload = {
                "message": {
                    "data": {
                        "title": title, # notification title
                        "message": message, # notification msg
                        "timestamp": timestamp, # event's local timestamp
                        "status": str(status) # true if alarm is on
                    },
                    "token": token # user
                }
            }
            logging.info(f"FCM: sending to {key}: {title} {message} {timestamp} {status}")
            fcm_response = requests.post(self.fcm_url, headers=self.fcm_headers, json=fcm_payload)
            if fcm_response.status_code == 200:
                logging.info(f"FCM: sent to {key}: {fcm_payload}")
            else:
                logging.error(f"FCM: failed to send message to user.")
                logging.error(f"{fcm_response.status_code} : {fcm_response.text}")
    
    
    '''
    Handles received POST requests
    '''
    def do_POST(self):
        logging.info("Received POST.")
        
        try:
            # Require Content-Length
            if not ("Content-Length" in self.headers):
                self.respond_error(411, "Length required (missing Content-Length).")
                return
            
            data_length = int(self.headers["Content-Length"])
            data = self.rfile.read(data_length)
            logging.info(f"{data.decode('utf-8')}")
            
            # Require JSON payload
            try:
                json_data = json.loads(data)
            except Exception as e:
                self.respond_error(400, f"Bad request (failed to load JSON content; Content-Type={self.headers['Content-Type']}).")
                return
            
            # Require 'keys', 'message' and 'timestamp' JSON fields, and a correct key
            if not ("key" in json_data and "message" in json_data and "timestamp" in json_data \
                and json_data["key"] in self.users):
                self.respond_error(400, "Bad request (incorrect JSON fields, or incorrect key).")
                return
            
            key = json_data["key"]
            message = json_data["message"]
            timestamp = json_data["timestamp"]
            
            # Require correct epoch timestamp
            try:
                i = datetime.fromtimestamp(int(timestamp))
            except Exception as e:
                self.respond_error(400, f"Bad request (incorrect timestamp: {e}).")
                return

            #
            logging.info(f"Correct request. Executing {message}...")
            match message:
                # Arduino
                case "alarm_is_on":
                    self.must_turn_alarm_off[key] = False
                    self.send_fcm(key, "Alarme déclenchée !", f"Activée à {datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H:%M:%S')}.", timestamp, True)
                    self.send_response(200)
                    self.end_headers()
                    logging.info(f"OK.")
                case "alarm_is_off":
                    self.must_turn_alarm_off[key] = False
                    response = self.send_fcm(key, "Alarme stoppée !", f"Désactivée à {datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H:%M:%S')}.", timestamp, False)
                    self.send_response(200)
                    self.end_headers()
                    logging.info(f"OK.")
                case "check_if_turn_off":
                    self.send_response(200)
                    self.end_headers()
                    response = {"message": self.must_turn_alarm_off[key]}
                    self.wfile.write(json.dumps(response).encode("utf-8"))
                    logging.info(f"OK. ({'asked to turn off' if self.must_turn_alarm_off[key] else 'no order'}.")
                # App
                case "set_alarm_off":
                    self.must_turn_alarm_off[key] = True # TODO: stocker le timestamp pour ne pas ordonner d'arrêter une alarme pas encore déclenchée
                    self.send_response(200)
                    self.end_headers()
                    logging.info(f"OK.")
                case "add_phone":
                    if "data" not in json_data:
                        self.respond_error(400, "Bad request (incorrect JSON fields, or incorrect key).")
                        return
                    new_token = json_data["data"]
                    self.users[key].append(new_token)
                    self.send_response(200)
                    self.end_headers()
                    logging.info(f"OK.")
                # Testing
                case "toggle_fcm":
                    self.block_fcm[key] = not self.block_fcm[key]
                    self.log(f"FCM is {'enabled' if not self.block_fcm[key] else 'disabled'}.")
                    self.send_response(200)
                    self.end_headers()
                    response = {"message": f"FCM is {'enabled' if not self.block_fcm[key] else 'disabled'}."}
                    self.wfile.write(json.dumps(response).encode("utf-8"))
                    logging.info(f"OK. (FCM is {'enabled' if not self.block_fcm[key] else 'disabled'})")
                # default
                case _:
                    self.respond_error(400, "Bad request (incorrect message).")
        except Exception as e:
            logging.error(f"Error while handling POST request.")
            logging.error(e)

if __name__ == "__main__":
    host = "localhost"
    port = 5000
    server = HTTPServer((host, port), RequestHandler)
    logging.info(f"Server started at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Manually shutting down server.")
        server.server_close()
    except Exception as e:
        logging.error("Unexpectedly shutting down server.")
        server.server_close()
