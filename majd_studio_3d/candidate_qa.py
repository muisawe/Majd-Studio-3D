"""Candidate silhouette QA against reference views; numpy/Pillow only."""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw


def ref_mask(image,size=256):
    image=image.convert("RGBA"); arr=np.asarray(image); alpha=arr[:,:,3]
    ys,xs=np.where(alpha>15)
    if not len(xs): return None
    crop=image.crop((xs.min(),ys.min(),xs.max()+1,ys.max()+1))
    scale=min((size*.9)/max(crop.width,1),(size*.9)/max(crop.height,1))
    crop=crop.resize((max(1,int(crop.width*scale)),max(1,int(crop.height*scale))),Image.Resampling.LANCZOS)
    canvas=Image.new("L",(size,size),0)
    canvas.paste(crop.getchannel("A"),((size-crop.width)//2,(size-crop.height)//2))
    return np.asarray(canvas)>20


def canonical_vertices(mesh):
    v=np.asarray(mesh.vertices,dtype=np.float64); v-=v.mean(axis=0); ext=np.ptp(v,axis=0)
    vertical=int(np.argmax(ext)); rem=[i for i in range(3) if i!=vertical]
    width=rem[0] if ext[rem[0]]>=ext[rem[1]] else rem[1]; depth=rem[1] if width==rem[0] else rem[0]
    out=np.column_stack([v[:,width],v[:,depth],v[:,vertical]]); h=np.ptp(out[:,2])
    if h>1e-9: out/=h
    return out


def mesh_mask(mesh,angle=0,size=256):
    v=canonical_vertices(mesh); t=math.radians(angle)
    x=v[:,0]*math.cos(t)-v[:,1]*math.sin(t); z=v[:,2]; pts=np.column_stack([x,z])
    mn,mx=pts.min(0),pts.max(0); span=np.maximum(mx-mn,1e-8)
    scale=min((size*.88)/span[0],(size*.88)/span[1])
    px=(pts[:,0]-(mn[0]+mx[0])/2)*scale+size/2; py=size/2-(pts[:,1]-(mn[1]+mx[1])/2)*scale
    proj=np.column_stack([px,py]); im=Image.new("L",(size,size),0); draw=ImageDraw.Draw(im)
    faces=np.asarray(mesh.faces)
    if len(faces)>50000: faces=faces[::max(1,len(faces)//50000)]
    for face in faces: draw.polygon([(float(proj[i,0]),float(proj[i,1])) for i in face],fill=255)
    return np.asarray(im)>0


def iou(a,b):
    if a is None or b is None: return None
    u=np.logical_or(a,b).sum(); return float(np.logical_and(a,b).sum()/u) if u else None


def best_iou(gen,ref):
    vals=[iou(gen,ref),iou(gen,np.fliplr(ref))]; vals=[v for v in vals if v is not None]
    return max(vals) if vals else None


def score_candidate(mesh,refs):
    scores=[]
    for key,angle in (("front",0),("back",180),("left",90),("right",-90)):
        if key in refs:
            s=best_iou(mesh_mask(mesh,angle),ref_mask(refs[key]));
            if s is not None: scores.append(s)
    if "threeq" in refs:
        r=ref_mask(refs["threeq"]); scores.append(max(best_iou(mesh_mask(mesh,45),r) or 0,best_iou(mesh_mask(mesh,-45),r) or 0))
    return float(np.mean(scores)) if scores else 0.0
