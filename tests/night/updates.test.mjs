import test from 'node:test';
import assert from 'node:assert/strict';
import {newerVersion,releaseUpdate} from '../../src/night/UpdateModel.mjs';
const digest='a'.repeat(64);
const release=(version,number=1)=>{const tag=`v${version}-preview.${number}`,prefix=`https://github.com/1atex/night/releases/download/${tag}/`;return {tag_name:tag,assets:[{name:`night-browser-${version}-preview-windows-x64-setup.exe`,browser_download_url:prefix+`night-browser-${version}-preview-windows-x64-setup.exe`,digest:'sha256:'+digest},{name:`night-browser-${version}-preview-windows-x64.zip`,browser_download_url:prefix+`night-browser-${version}-preview-windows-x64.zip`,digest:'sha256:'+digest}]};};
test('updater uses the new repository and GitHub asset digest with only installer and portable assets',()=>{
 assert.equal(newerVersion('0.18.2','0.18.1'),true);assert.equal(newerVersion('0.9.0','0.18.2'),false);
 assert.equal(releaseUpdate([release('0.18.1')],'0.18.2'),null);
 const candidate=releaseUpdate([release('0.18.2')],'0.18.1');assert.equal(candidate.sha256,digest);assert.equal(candidate.sums,undefined);assert.match(candidate.url,/github\.com\/1atex\/night\/releases/);
 assert.equal(releaseUpdate([release('0.18.2',2)],'0.18.2').tag,'v0.18.2-preview.2');
 assert.equal(releaseUpdate([release('0.18.2',3)],'0.18.2',3),null);
});
test('updater rejects a foreign or old repository, absent/malformed digests and draft releases',()=>{
 for(const url of ['https://example.test/setup.exe',release('0.18.2').assets[0].browser_download_url.replace('/1atex/night/','/1atex/night-browser/')]){const value=release('0.18.2');value.assets[0].browser_download_url=url;assert.equal(releaseUpdate([value],'0.18.1'),null);}
 for(const value of [null,undefined,'sha256:123','md5:'+digest,'sha256:'+digest+'0']){const item=release('0.18.2');item.assets[0].digest=value;assert.equal(releaseUpdate([item],'0.18.1'),null);}
 const draft={...release('0.18.2'),draft:true};assert.equal(releaseUpdate([draft],'0.18.1'),null);
 const upper=release('0.18.2');upper.assets[0].digest='SHA256:'+digest.toUpperCase();assert.equal(releaseUpdate([upper],'0.18.1').sha256,digest);
});
