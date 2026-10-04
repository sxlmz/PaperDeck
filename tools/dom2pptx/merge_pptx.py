# -*- coding: utf-8 -*-
"""合并 19 个单页 pptx -> 一个 19 页 pptx（OPC zip 级合并）。

单页结构（dom-to-pptx 输出）：
  ppt/slides/slide1.xml + _rels/slide1.xml.rels
  ppt/notesSlides/notesSlide1.xml + _rels
  ppt/media/image-1-N.{png,svg}
合并要点：
  1) 每页 media 重命名为 img_{page}_{n}.ext（避免跨页同名冲突）
  2) slide rels 的 rId 保留（slide XML 里 r:embed 引用它们），只改 media Target
  3) [Content_Types] 追加 slide/notesSlide Override（media 扩展走 Default 已有）
  4) presentation.xml sldIdLst 追加 sldId；presentation.xml.rels 追加 rId
"""
import io, os, re, shutil, sys, zipfile
from lxml import etree

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
      "pkg": "http://schemas.openxmlformats.org/package/2006/relationships"}

def read(path, z: zipfile.ZipFile):
    return z.read(path)

def write(zout: zipfile.ZipFile, name: str, data: bytes):
    zout.writestr(name, data)

def _page_num(name: str) -> int:
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else 0

def main():
    # 用法: py merge_pptx.py <单页pptx目录> [输出路径]
    src_dir = sys.argv[1] if len(sys.argv) > 1 else "dom2pptx_pages"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "deck_merged.pptx"
    pages = sorted([f for f in os.listdir(src_dir) if f.lower().endswith(".pptx")],
                   key=_page_num)
    if not pages:
        print("无单页 pptx 可合并:", src_dir)
        sys.exit(1)
    print(f"合并 {len(pages)} 页: {pages[0]} ... {pages[-1]} -> {out_path}")
    if os.path.exists(out_path):
        os.remove(out_path)

    base = zipfile.ZipFile(os.path.join(src_dir, pages[0]))
    files = {n: base.read(n) for n in base.namelist()}
    base.close()

    # presentation.xml 与 rels（以第 1 页为基底，追加其余 18 页）
    pres_xml = files["ppt/presentation.xml"]
    pres_rels = files["ppt/_rels/presentation.xml.rels"]
    ct_xml = files["[Content_Types].xml"]

    root_pres = etree.fromstring(pres_xml)
    sldIdLst = root_pres.find(".//p:sldIdLst", NS)
    if sldIdLst is None:
        sldIdLst = etree.SubElement(root_pres, etree.QName(NS["p"], "sldIdLst"))

    # 现有 sldId 的 rId 集合（第 1 页占用的）
    used_ids = set()
    max_id = 255
    for s in sldIdLst.findall("p:sldId", NS):
        used_ids.add(s.get("id"))
        try:
            max_id = max(max_id, int(s.get("id")))
        except (TypeError, ValueError):
            pass

    rels_root = etree.fromstring(pres_rels)
    # 用大偏移分配追加 rId（rId100+），保证不与基底任何关系重叠
    rel_id_num = 100
    def _rels_el(tag):
        return etree.QName(NS["pkg"], tag)

    ct_root = etree.fromstring(ct_xml)
    # 已有 Override 集合（避免重复）
    existing_ov = {o.get("PartName") for o in ct_root.findall("ct:Override", NS)}

    media_count = 0

    for idx, pg in enumerate(pages[1:], start=2):
        pnum = idx  # 1-based slide number
        z = zipfile.ZipFile(os.path.join(src_dir, pg))
        slide_xml = z.read("ppt/slides/slide1.xml")
        slide_rels = z.read("ppt/slides/_rels/slide1.xml.rels")
        notes_xml = z.read("ppt/notesSlides/notesSlide1.xml")
        notes_rels = z.read("ppt/notesSlides/_rels/notesSlide1.xml.rels")
        media_map = {}  # old name -> new name

        # 1) 重写 slide rels：media Target 重命名
        # 注意：Relationship 元素属于 **pkg** 命名空间（package/2006/relationships），
        # 不是 r（officeDocument/2006/relationships，那是 r:id 属性用的）。
        # 曾误用 "r:Relationship" 查找：找不到任何元素 -> media 不复制、
        # Target 不重写，合并后所有图片引用全部悬空（实测 19 页只剩 1 张图）。
        sr = etree.fromstring(slide_rels)
        for rel in sr.findall("pkg:Relationship", NS):
            tgt = rel.get("Target") or ""
            if "media/" in tgt:
                base_name = os.path.basename(tgt)
                media_count += 1
                new_name = f"img_{pnum}_{media_count}_{base_name}"
                media_map[base_name] = new_name
                rel.set("Target", f"../media/{new_name}")
                # 复制 media 二进制
                media_bytes = z.read(f"ppt/media/{base_name}")
                files[f"ppt/media/{new_name}"] = media_bytes
                # Content_Types Default 扩展（png/svg 通常已存在；保险加）
                ext = base_name.rsplit(".", 1)[-1]
        files[f"ppt/slides/slide{pnum}.xml"] = slide_xml
        files[f"ppt/slides/_rels/slide{pnum}.xml.rels"] = etree.tostring(sr)

        # 2) notesSlide：重命名 + rels 里 media（若有）
        nr = etree.fromstring(notes_rels)
        for rel in nr.findall("pkg:Relationship", NS):
            tgt = rel.get("Target") or ""
            if "media/" in tgt:
                base_name = os.path.basename(tgt)
                media_count += 1
                new_name = f"img_{pnum}_{media_count}_{base_name}"
                media_map.setdefault(base_name, new_name)
                rel.set("Target", f"../media/{new_name}")
                try:
                    files[f"ppt/media/{new_name}"] = z.read(f"ppt/media/{base_name}")
                except KeyError:
                    pass
        files[f"ppt/notesSlides/notesSlide{pnum}.xml"] = notes_xml
        files[f"ppt/notesSlides/_rels/notesSlide{pnum}.xml.rels"] = etree.tostring(nr)

        # 3) [Content_Types] Override
        ov = etree.SubElement(ct_root, etree.QName(NS["ct"], "Override"))
        ov.set("PartName", f"/ppt/slides/slide{pnum}.xml")
        ov.set("ContentType", "application/vnd.openxmlformats-officedocument.presentationml.slide+xml")
        ov2 = etree.SubElement(ct_root, etree.QName(NS["ct"], "Override"))
        ov2.set("PartName", f"/ppt/notesSlides/notesSlide{pnum}.xml")
        ov2.set("ContentType", "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml")

        # 4) presentation.xml.rels 追加 rId -> slide
        rel_id_num += 1
        rid = f"rId{rel_id_num}"
        rel = etree.SubElement(rels_root, _rels_el("Relationship"))
        rel.set("Id", rid)
        rel.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide")
        rel.set("Target", f"slides/slide{pnum}.xml")

        # 5) sldIdLst 追加
        sld = etree.SubElement(sldIdLst, etree.QName(NS["p"], "sldId"))
        max_id += 1
        sld.set("id", str(max_id))
        sld.set(etree.QName(NS["r"], "id"), rid)
        z.close()

    files["ppt/presentation.xml"] = etree.tostring(root_pres)
    files["ppt/_rels/presentation.xml.rels"] = etree.tostring(rels_root)
    files["[Content_Types].xml"] = etree.tostring(ct_root)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zo:
        for n, data in files.items():
            zo.writestr(n, data)
    print("合并完成:", out_path, "总文件数:", len(files))

if __name__ == "__main__":
    main()
