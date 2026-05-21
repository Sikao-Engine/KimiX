#!/usr/bin/env python3
"""
PDF to Markdown Converter

Converts PDF documents to Markdown format with support for:
- Text extraction with formatting preservation
- Table extraction
- Image extraction with optional OCR
- Heading detection
- List detection

Requirements:
    pip install pymupdf pillow pytesseract pdfplumber

Optional (for OCR):
    pip install pytesseract
    # And install Tesseract OCR: https://github.com/UB-Mannheim/tesseract/wiki

Usage:
    python pdf_to_md.py input.pdf -o output.md
    python pdf_to_md.py input.pdf --images --ocr
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional, List, Tuple

from kimix.base import print  # noqa: F811 - use base.print for file/flush support

# Optional imports - will be checked at runtime
try:
    import fitz
except ImportError:
    fitz = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


def check_dependencies():
    """Check if required dependencies are installed."""
    missing = []
    
    try:
        import fitz  # pymupdf
    except ImportError:
        missing.append("pymupdf")
    
    try:
        from PIL import Image
    except ImportError:
        missing.append("pillow")
    
    if missing:
        return False
    return True


def extract_text_with_fitz(pdf_path: str, page_num: int) -> str:
    """Extract text from a PDF page using PyMuPDF."""
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    
    # Get text with formatting hints
    text = page.get_text("markdown")
    if not text.strip():
        # Fallback to plain text if markdown format is empty
        text = page.get_text("text")
    
    doc.close()
    return text


def extract_text_blocks(pdf_path: str, page_num: int) -> List[dict]:
    """Extract text blocks with their properties for better formatting."""
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    
    blocks = page.get_text("dict")["blocks"]
    doc.close()
    
    return blocks


def detect_heading(text: str, font_size: float, avg_font_size: float) -> str:
    """Detect if text is a heading based on font size and content."""
    text = text.strip()
    if not text:
        return text
    
    # Heading patterns
    heading_patterns = [
        r'^Chapter\s+\d+',
        r'^\d+\.[\d.]*\s+',  # Numbered sections like 1. 1.1 1.1.1
        r'^(?:Section|Appendix)\s+\d+',
    ]
    
    # Check if text matches heading patterns
    is_pattern_heading = any(re.match(pattern, text, re.IGNORECASE) for pattern in heading_patterns)
    
    # Check font size (significantly larger than average)
    is_size_heading = font_size > avg_font_size * 1.2
    
    # Check if text is all caps and short (likely a heading)
    is_caps_heading = text.isupper() and len(text) < 100 and len(text.split()) < 10
    
    if is_pattern_heading or is_size_heading or is_caps_heading:
        # Determine heading level
        if font_size > avg_font_size * 1.8 or text.isupper():
            return f"# {text}"
        elif font_size > avg_font_size * 1.5:
            return f"## {text}"
        elif font_size > avg_font_size * 1.3:
            return f"### {text}"
        else:
            return f"#### {text}"
    
    return text


def process_text_blocks(blocks: List[dict]) -> str:
    """Process text blocks and convert to markdown."""
    markdown_lines = []
    
    # Calculate average font size
    font_sizes = []
    for block in blocks:
        if "lines" in block:
            for line in block["lines"]:
                for span in line["spans"]:
                    font_sizes.append(span["size"])
    
    avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 12
    
    prev_y = 0
    for block in blocks:
        if "lines" not in block:
            continue
        
        block_text = []
        block_font_size = avg_font_size
        
        for line in block["lines"]:
            line_text = ""
            for span in line["spans"]:
                text = span["text"]
                flags = span["flags"]
                block_font_size = max(block_font_size, span["size"])
                
                # Apply inline formatting (strip leading/trailing spaces first)
                stripped_text = text.strip()
                if flags & 2**4:  # Bold
                    stripped_text = f"**{stripped_text}**"
                if flags & 2**5:  # Italic
                    stripped_text = f"*{stripped_text}*"
                if flags & 2**6:  # Underline
                    stripped_text = f"<u>{stripped_text}</u>"
                # Preserve original spacing
                if text.startswith(' '):
                    stripped_text = ' ' + stripped_text
                if text.endswith(' '):
                    stripped_text = stripped_text + ' '
                text = stripped_text
                
                line_text += text
            
            if line_text.strip():
                block_text.append(line_text)
        
        if block_text:
            text = " ".join(block_text).strip()
            
            # Detect lists
            if re.match(r'^[•·\-\*•]\s', text):
                text = "- " + re.sub(r'^[•·\-\*•]\s*', '', text)
            elif re.match(r'^\d+[.\)]\s', text):
                text = re.sub(r'^(\d+)[.\)]\s*', r'\1. ', text)
            else:
                # Check for heading
                text = detect_heading(text, block_font_size, avg_font_size)
            
            # Add paragraph breaks based on vertical spacing
            if prev_y > 0 and block["bbox"][1] - prev_y > avg_font_size * 1.5:
                markdown_lines.append("")
            
            markdown_lines.append(text)
            prev_y = block["bbox"][3]
    
    return "\n".join(markdown_lines)


def extract_tables(pdf_path: str, page_num: int) -> List[str]:
    """Extract tables from PDF page and convert to markdown."""
    if pdfplumber is None:
        return []
    
    tables_md = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_num < len(pdf.pages):
                page = pdf.pages[page_num]
                tables = page.extract_tables()
                
                for table in tables:
                    if not table:
                        continue
                    
                    md_table = []
                    
                    # Header row
                    header = table[0]
                    md_table.append("| " + " | ".join(str(cell or "") for cell in header) + " |")
                    md_table.append("| " + " | ".join(["---"] * len(header)) + " |")
                    
                    # Data rows
                    for row in table[1:]:
                        md_table.append("| " + " | ".join(str(cell or "") for cell in row) + " |")
                    
                    tables_md.append("\n".join(md_table))
    except ImportError:
        return []
    
    return tables_md


def extract_images(pdf_path: str, page_num: int, output_dir: str, ocr: bool = False) -> List[str]:
    """Extract images from PDF page and optionally run OCR."""
    from PIL import Image
    import io
    
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    image_list = page.get_images()
    
    image_refs = []
    
    for img_index, img in enumerate(image_list, start=1):
        xref = img[0]
        base_image = doc.extract_image(xref)
        image_bytes = base_image["image"]
        image_ext = base_image["ext"]
        
        # Save image
        image_filename = f"page_{page_num + 1}_img_{img_index}.{image_ext}"
        image_path = os.path.join(output_dir, image_filename)
        
        with open(image_path, "wb") as f:
            f.write(image_bytes)
        
        # Create markdown reference
        alt_text = f"Image {img_index} on page {page_num + 1}"
        
        # Optional OCR
        if ocr:
            try:
                import pytesseract
                pil_image = Image.open(io.BytesIO(image_bytes))
                ocr_text = pytesseract.image_to_string(pil_image).strip()
                if ocr_text:
                    alt_text = ocr_text[:100] + "..." if len(ocr_text) > 100 else ocr_text
            except Exception:
                pass
        
        image_refs.append(f"![{alt_text}]({image_filename})")
    
    doc.close()
    return image_refs


def clean_markdown(text: str) -> str:
    """Clean up and format the markdown text."""
    # Remove excessive whitespace
    text = re.sub(r'\n{4,}', '\n\n\n', text)
    
    # Fix common issues
    text = re.sub(r'\*\*\*\*', '**', text)  # Fix bold formatting
    text = re.sub(r'\*\*\s*\*\*', '', text)  # Remove empty bold
    
    # Ensure proper spacing around headers
    text = re.sub(r'^(#{1,6}\s.+)$', r'\n\1\n', text, flags=re.MULTILINE)
    
    # Clean up multiple spaces
    text = re.sub(r' +', ' ', text)
    
    return text.strip()


def pdf_to_markdown(
    pdf_path: str,
    output_path: Optional[str] = None,
    extract_imgs: bool = False,
    ocr: bool = False,
    extract_tbls: bool = True,
    page_range: Optional[Tuple[int, int]] = None
) -> str:
    """
    Convert a PDF file to Markdown.
    
    Args:
        pdf_path: Path to the PDF file
        output_path: Path for the output markdown file (optional)
        extract_imgs: Whether to extract images
        ocr: Whether to run OCR on images
        extract_tbls: Whether to extract tables
        page_range: Tuple of (start, end) page numbers (0-indexed, inclusive)
    
    Returns:
        The markdown content as a string
    """
    pdf_path = os.path.abspath(pdf_path)
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    
    
    # Setup output directory
    if output_path:
        output_dir = os.path.dirname(os.path.abspath(output_path)) or "."
        base_name = os.path.splitext(os.path.basename(output_path))[0]
    else:
        output_dir = os.path.dirname(pdf_path) or "."
        base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    
    # Create images directory if needed
    images_dir = os.path.join(output_dir, f"{base_name}_images")
    if extract_imgs:
        os.makedirs(images_dir, exist_ok=True)
    
    # Open PDF
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    doc.close()
    
    # Determine page range
    start_page = page_range[0] if page_range else 0
    end_page = page_range[1] if page_range else total_pages - 1
    start_page = max(0, start_page)
    end_page = min(total_pages - 1, end_page)
    
    markdown_sections = []
    
    for page_num in range(start_page, end_page + 1):
        print(f"Processing page {page_num + 1}/{end_page + 1}...", file=sys.stderr)
        
        page_content = [f"\n<!-- Page {page_num + 1} -->\n"]
        
        # Extract text blocks
        blocks = extract_text_blocks(pdf_path, page_num)
        text_content = process_text_blocks(blocks)
        
        if text_content.strip():
            page_content.append(text_content)
        
        # Extract tables
        if extract_tbls:
            try:
                tables = extract_tables(pdf_path, page_num)
                for table_md in tables:
                    page_content.append(f"\n{table_md}\n")
            except Exception as e:
                print(f"Warning: Could not extract tables from page {page_num + 1}: {e}", file=sys.stderr)
        
        # Extract images
        if extract_imgs:
            try:
                images = extract_images(pdf_path, page_num, images_dir, ocr)
                for img_md in images:
                    page_content.append(f"\n{img_md}\n")
            except Exception as e:
                print(f"Warning: Could not extract images from page {page_num + 1}: {e}", file=sys.stderr)
        
        markdown_sections.append("\n".join(page_content))
    
    # Combine all content
    markdown_content = "\n\n".join(markdown_sections)
    
    # Clean up
    markdown_content = clean_markdown(markdown_content)
    
    # Add metadata header
    metadata = f"""---
title: {base_name}
pages: {start_page + 1} to {end_page + 1} of {total_pages}
generated: true
---

"""
    markdown_content = metadata + markdown_content
    
    # Write to file if output path specified
    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(markdown_content)
        print(f"Markdown saved to: {output_path}", file=sys.stderr)
    
    return markdown_content


def main():
    parser = argparse.ArgumentParser(
        description="Convert PDF documents to Markdown format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s document.pdf                    # Output to stdout
    %(prog)s document.pdf -o output.md       # Save to file
    %(prog)s document.pdf --images           # Extract images
    %(prog)s document.pdf --images --ocr     # Extract images with OCR
    %(prog)s document.pdf -p 0-5             # Convert only pages 1-6
    %(prog)s document.pdf -p 5               # Convert only page 6
        """
    )
    
    parser.add_argument("pdf", help="Input PDF file path")
    parser.add_argument("-o", "--output", help="Output markdown file path")
    parser.add_argument("-p", "--pages", help="Page range (e.g., '0-5' or '3' for single page)")
    parser.add_argument("--images", action="store_true", help="Extract images from PDF")
    parser.add_argument("--ocr", action="store_true", help="Run OCR on extracted images (requires pytesseract)")
    parser.add_argument("--no-tables", action="store_true", help="Skip table extraction")
    parser.add_argument("--format", choices=["simple", "advanced"], default="advanced",
                        help="Conversion mode: 'simple' for basic text, 'advanced' for formatted output")
    
    args = parser.parse_args()
    
    # Parse page range
    page_range = None
    if args.pages:
        if '-' in args.pages:
            start, end = args.pages.split('-', 1)
            page_range = (int(start), int(end))
        else:
            page_num = int(args.pages)
            page_range = (page_num, page_num)
    
    # Simple mode: just extract text
    if args.format == "simple":
        if fitz is None:
            print("Error: pymupdf is required. Install with: pip install pymupdf")
            sys.exit(1)
        
        doc = fitz.open(args.pdf)
        start = page_range[0] if page_range else 0
        end = page_range[1] if page_range else len(doc) - 1
        
        text = ""
        for i in range(start, end + 1):
            text += doc[i].get_text() + "\n\n"
        doc.close()
        
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(text)
            print(f"Saved to: {args.output}")
        else:
            print(text)
        
        sys.exit(0)
    
    # Advanced mode
    try:
        markdown = pdf_to_markdown(
            pdf_path=args.pdf,
            output_path=args.output,
            extract_imgs=args.images,
            ocr=args.ocr,
            extract_tbls=not args.no_tables,
            page_range=page_range
        )
        
        if not args.output:
            print(markdown)
            
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error converting PDF: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
