"""Bounded loopback image-upload runner. Fixed local model, no arbitrary commands."""
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image
from inspection_engine.core import Config, analyze, plan, positive, validate_frame
from inspection_engine.perception import FrozenPerception
from sem_efficiency.core import digest, now, write

MAX_BODY=12<<20
MAX_IMAGE=8<<20
MAX_PIXELS=1024*1024
MAX_CANDIDATES=200


def decode_request(payload, model_sha):
    keys={'image_base64','candidates','quality','baseline_source','budget_s'}
    if not isinstance(payload,dict) or set(payload)!=keys:
        raise ValueError('Expected acquired image, candidates, quality, baseline source and budget only')
    if not isinstance(payload['image_base64'],str):
        raise ValueError('image_base64 must be a base64 string')
    raw=base64.b64decode(payload['image_base64'],validate=True)
    if not 0<len(raw)<=MAX_IMAGE:
        raise ValueError('Acquired image limit: 8 MiB')
    with Image.open(io.BytesIO(raw)) as im:
        if im.format not in ('PNG','JPEG') or im.mode!='L':
            raise ValueError('Upload an 8-bit grayscale PNG/JPEG; color conversion is not implicit')
        w,h=im.size
        if w*h>MAX_PIXELS:
            raise ValueError('Local CPU development limit: 1,048,576 pixels per frame')
        im.verify()
    # Reading PNG EXIF can load its stream, so metadata checks use a separate open.
    with Image.open(io.BytesIO(raw)) as im:
        if im.getexif().get(274,1)!=1 or getattr(im,'n_frames',1)!=1:
            raise ValueError('Use one frame with no EXIF rotation; candidate coordinates must match the pixel matrix')
    cs=payload['candidates']
    if not isinstance(cs,list) or len(cs)>MAX_CANDIDATES:
        raise ValueError('At most 200 incumbent candidates per development frame')
    sha=hashlib.sha256(raw).hexdigest()
    f={'schema_version':1,'frame_id':'upload:'+sha[:24],'width':w,'height':h,
       'image_sha256':sha,'model_sha256':model_sha,'data_mode':'instrument_shadow',
       'baseline_source':payload['baseline_source'],'quality':payload['quality'],'candidates':cs}
    f=validate_frame(f)
    for c in f['candidates']:
        if c['review_bound_s']>3600:
            raise ValueError('Review bounds must be <= 3600 seconds')
    budget=positive(payload['budget_s'],'budget_s',zero=True)
    if budget>3600:
        raise ValueError('Review allowance must be <= 3600 seconds')
    return raw,f,budget


class EngineService:
    def __init__(self,model_root,output,example_frame,example_image):
        self.backend=FrozenPerception(model_root,2)
        self.output=Path(output).resolve()
        self.output.mkdir(parents=True,exist_ok=True)
        self.lock=threading.Lock()
        self.model_sha=self.backend.frozen['model']['sha256']
        self.config=Config()
        f=validate_frame(json.loads(Path(example_frame).read_text()))
        if digest(example_image)!=f['image_sha256'] or f['data_mode']!='real_image_development' or f['model_sha256']!=self.model_sha:
            raise ValueError('Example must be a verified development image')
        self.example={'image_base64':base64.b64encode(Path(example_image).read_bytes()).decode(),
                      'candidates':f['candidates'],'quality':'valid',
                      'baseline_source':'Software proxy example — not commercial equipment outputs','budget_s':120}

    def run(self,payload):
        raw,frame,budget=decode_request(payload,self.model_sha)
        job='run-'+uuid.uuid4().hex
        out=self.output/job
        out.mkdir()
        source=out/'acquired-image.bin'
        source.write_bytes(raw)
        write(out/'frame.json',frame)
        write(out/'status.json',{'status':'started','created_at':now()})
        try:
            p,receipt=self.backend.predict(source,frame['image_sha256'])
            result=analyze(frame,p,config=self.config)
            scheduled=plan(result,budget)
            np.save(out/'probability.npy',p,allow_pickle=False)
            receipt['output_probability_sha256']=digest(out/'probability.npy')
            receipt['frame_sha256']=digest(out/'frame.json')
            write(out/'inference-receipt.json',receipt)
            write(out/'analysis.json',result)
            write(out/'plan.json',scheduled)
            write(out/'status.json',{'status':'complete','created_at':now(),
                                    'hardware_connected':False,'commercial_validated':False})
            return {'status':'complete','run_id':job,'analysis':result,'plan':scheduled,'inference':receipt,
                    'artifact_hashes':{n:digest(out/n) for n in ('analysis.json','plan.json','inference-receipt.json')}}
        except Exception as e:
            write(out/'status.json',{'status':'failed','created_at':now(),'error':str(e)})
            raise


def make_handler(service,page,port):
    allowed_host=f'127.0.0.1:{port}'
    origin=f'http://{allowed_host}'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def send_bytes(self,code,body,mime):
            self.send_response(code)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(body)

        def send_json(self,code,data):
            self.send_bytes(code,json.dumps(data,allow_nan=False).encode(),'application/json; charset=utf-8')

        def guard(self,write_request=False):
            if self.headers.get('Host')!=allowed_host:
                self.send_json(403,{'error':'Use the loopback address printed by the runner'})
                return False
            provided=self.headers.get('Origin')
            if provided is not None and provided!=origin:
                self.send_json(403,{'error':'Cross-origin access refused'})
                return False
            if write_request and self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                self.send_json(415,{'error':'application/json required'})
                return False
            return True

        def do_GET(self):
            if not self.guard():
                return
            path=urlsplit(self.path).path
            if path in ('/','/inspection-engine.html'):
                self.send_bytes(200,page.read_bytes(),'text/html; charset=utf-8')
            elif path=='/api/info':
                self.send_json(200,{'model_sha256':service.model_sha,'hardware_connected':False,
                                   'commercial_validated':False,'max_pixels':MAX_PIXELS,
                                   'max_image_bytes':MAX_IMAGE,'max_candidates':MAX_CANDIDATES})
            elif path=='/api/example':
                self.send_json(200,service.example)
            else:
                self.send_json(404,{'error':'Unknown fixed endpoint'})

        def do_POST(self):
            if not self.guard(True):
                return
            if urlsplit(self.path).path!='/api/analyze':
                self.send_json(404,{'error':'Unknown fixed endpoint'})
                return
            if self.headers.get('Transfer-Encoding'):
                self.send_json(400,{'error':'Transfer-Encoding unsupported'})
                return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=MAX_BODY:
                    raise ValueError('Request size limit exceeded or missing content length')
                self.connection.settimeout(15)
                raw=self.rfile.read(size)
                if len(raw)!=size:
                    raise ValueError('Incomplete request body')
                payload=json.loads(raw)
            except (ValueError,TimeoutError) as e:
                self.send_json(400,{'error':str(e)})
                return
            if not service.lock.acquire(blocking=False):
                self.send_json(409,{'error':'A local analysis is already running'})
                return
            try:
                result=service.run(payload)
                self.send_json(200,result)
            except (ValueError,KeyError,TypeError) as e:
                self.send_json(400,{'error':str(e)})
            except Exception:
                self.send_json(500,{'error':'Local analysis failed; inspect the preserved run status'})
            finally:
                service.lock.release()

    return Handler


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--model-root',required=True);p.add_argument('--out',required=True)
    p.add_argument('--example-frame',required=True);p.add_argument('--example-image',required=True)
    p.add_argument('--port',type=int,default=8877)
    a=p.parse_args()
    if not 1024<=a.port<=65535:
        raise ValueError('port must be in 1024..65535')
    service=EngineService(a.model_root,a.out,a.example_frame,a.example_image)
    page=Path(__file__).resolve().parents[1]/'web'/'post-hackathon-engine'/'inspection-engine.html'
    http=ThreadingHTTPServer(('127.0.0.1',a.port),make_handler(service,page,a.port))
    print(json.dumps({'status':'ready','url':f'http://127.0.0.1:{a.port}/inspection-engine.html',
                      'model_sha256':service.model_sha,'hardware_connected':False}),flush=True)
    http.serve_forever()


if __name__=='__main__':
    main()
