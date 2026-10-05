"""Live runner boundary checks without loading a model or reading original test data."""
import base64
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib import request, error

from PIL import Image
from scripts.serve_inspection_engine import decode_request, make_handler


def payload(mode='L'):
    b=io.BytesIO()
    Image.new(mode,(20,20)).save(b,format='PNG')
    return {'image_base64':base64.b64encode(b.getvalue()).decode(),'candidates':[],
            'quality':'valid','baseline_source':'test fixture','budget_s':120}


class ServerTests(unittest.TestCase):
    def test_native_image_and_exact_schema(self):
        raw,f,b=decode_request(payload(),'b'*64)
        self.assertEqual((f['width'],f['height']),(20,20))
        self.assertEqual(b,120)
        self.assertTrue(f['frame_id'].startswith('upload:'))
        for extra in ('command','image_path','truth','model_root'):
            with self.assertRaises(ValueError):
                decode_request({**payload(),extra:'forbidden'},'b'*64)

    def test_limits_and_color_not_silently_converted(self):
        with self.assertRaises(ValueError):
            decode_request(payload('RGB'),'b'*64)
        with self.assertRaises(ValueError):
            decode_request({**payload(),'budget_s':3601},'b'*64)
        with self.assertRaises(ValueError):
            decode_request({**payload(),'image_base64':'not base64'},'b'*64)

    def test_coordinate_box_validation(self):
        c={'candidate_id':'a','bbox_px':[0,0,21,20],'review_bound_s':10}
        with self.assertRaises(ValueError):
            decode_request({**payload(),'candidates':[c]},'b'*64)

    def test_loopback_origin_fixed_routes_and_single_job(self):
        class Fake:
            model_sha='b'*64
            lock=threading.Lock()
            example=payload()
            def run(self,p):
                return {'status':'complete'}
        service=Fake()
        with tempfile.TemporaryDirectory() as d:
            page=Path(d)/'ui.html';page.write_text('<html>fixture</html>')
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(service,page,0))
            port=server.server_port
            server.RequestHandlerClass=make_handler(service,page,port)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{port}'
            try:
                self.assertEqual(json.load(request.urlopen(url+'/api/info'))['model_sha256'],'b'*64)
                for path in ('/../../secret','/api/command'):
                    with self.assertRaises(error.HTTPError) as caught:
                        request.urlopen(url+path)
                    self.assertEqual(caught.exception.code,404)
                req=request.Request(url+'/api/analyze',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://external.example'})
                with self.assertRaises(error.HTTPError) as caught:
                    request.urlopen(req)
                self.assertEqual(caught.exception.code,403)
                req=request.Request(url+'/api/analyze',data=b'{}',headers={'Content-Type':'application/json'})
                service.lock.acquire()
                try:
                    with self.assertRaises(error.HTTPError) as caught:
                        request.urlopen(req)
                    self.assertEqual(caught.exception.code,409)
                finally:
                    service.lock.release()
                self.assertEqual(json.load(request.urlopen(req))['status'],'complete')
            finally:
                server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    unittest.main()
