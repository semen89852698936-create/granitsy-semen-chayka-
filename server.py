"""Deployable standalone demo for Timeweb App Platform. Do not enable payments."""
import os
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from functools import partial
import json
class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        route=self.path.split('?')[0]
        if route == '/healthz':
            return self.json_response({'ok':True,'service':'snachala-uzhin','mode':'demo'})
        if route == '/api/status':
            return self.json_response({'published_recipes':21,'planned_recipes':1001,'payments_enabled':False})
        return super().do_GET()
    def json_response(self,data):
        body=json.dumps(data,ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
        super().end_headers()
if __name__=='__main__':
    port=int(os.environ.get('PORT','8080'))
    ThreadingHTTPServer(('0.0.0.0',port),partial(Handler,directory=str(Path(__file__).resolve().parent))).serve_forever()
