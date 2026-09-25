"""Observe the failed public comparison without modifying application behavior.

This is a diagnostic run, NOT replacement evidence for the failed release gate.
The original five-second assertion remains in the verifier source. We observe
for up to 30 seconds and record request/response/DOM timing to distinguish a
lost click, a server error, and a slow response. No request interception.
"""
from pathlib import Path
import argparse
import json

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--output', type=Path, default=Path('verification/public-diagnosis'))
    args=parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source=Path('scripts/verify_handoff_browser.py').read_text()
    needle="expect(page.locator('#handoffComparisonResult tbody tr')).to_have_count(6)"
    assert source.count(needle)==1
    source=source.replace(needle,"expect(page.locator('#handoffComparisonResult tbody tr')).to_have_count(6,timeout=30000)")
    needle="page.on('pageerror',lambda err:errors.append(str(err)))"
    assert source.count(needle)==1
    observer='''
                    import time
                    network=[];started=time.monotonic()
                    def record(kind,request,**extra):
                        network.append(dict(kind=kind,at=round(time.monotonic()-started,3),url=request.url,method=request.method,**extra))
                        (output/f'{mode}-{repetition}-network.json').write_text(json.dumps(network,ensure_ascii=False,indent=2))
                    page.on('request',lambda req:record('request',req))
                    page.on('response',lambda response:record('response',response.request,status=response.status))
                    page.on('requestfailed',lambda req:record('failed',req,failure=req.failure))
                    page.on('console',lambda msg:network.append(dict(kind='console',message=msg.text)))
'''
    source=source.replace(needle,needle+observer)
    needle="page.locator('nav [data-view=\"customer\"]').click();page.locator('#compareHandoff').click()"
    assert source.count(needle)==1
    source=source.replace(needle,needle+"\n                    (output/f'{mode}-{repetition}-after-click.json').write_text(json.dumps(page.evaluate('({busy,bodyBusy:document.body.classList.contains(\"busy\"),text:document.getElementById(\"handoffComparisonResult\").innerText})'),ensure_ascii=False,indent=2))")
    needle='''        except Exception as e:
            (output/'browser-failure.json')'''
    assert source.count(needle)==1
    source=source.replace(needle,'''        except Exception as e:
            if 'page' in locals() and not page.is_closed():
                page.screenshot(path=str(output/'failure.png'),full_page=True)
                (output/'failure-state.json').write_text(json.dumps(page.evaluate('({busy,bodyBusy:document.body.classList.contains("busy"),notice:document.getElementById("notice").innerText,text:document.getElementById("handoffComparisonResult").innerText})'),ensure_ascii=False,indent=2))
                context.close()
            (output/'browser-failure.json')''')
    namespace={'__name__':'diagnostic_verifier','__file__':'scripts/verify_handoff_browser.py'}
    exec(compile(source,'diagnostic_handoff.py','exec'),namespace)
    print(json.dumps(namespace['verify']('https://pickup-pact-demo.onrender.com',args.output,1,args.expected_commit),ensure_ascii=False,indent=2))
