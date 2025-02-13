import binascii
import io
from typing import List
import os

import pypdf
import ray
from pypdf import PdfReader

ray.init(
    runtime_env={"pip": ["langchain", "pypdf", "sentence_transformers", "transformers"]}
)

from ray.data.datasource import FileExtensionFilter

# Define the path to the folder containing the PDF files
pdf_folder_path = "/app/documents"  # Use the path in the container where the documents are mounted

# Filter out non-PDF files.
ds = ray.data.read_directory(pdf_folder_path, partition_filter=FileExtensionFilter("pdf"))

def convert_to_text(pdf_bytes: bytes):
    pdf_bytes_io = io.BytesIO(pdf_bytes)

    try:
        pdf_doc = PdfReader(pdf_bytes_io)
    except pypdf.errors.PdfStreamError:
        # Skip pdfs that are not readable.
        return []

    text = []
    for page in pdf_doc.pages:
        try:
            text.append(page.extract_text())
        except binascii.Error:
            # Skip all pages that are not parseable due to malformed characters.
            print("parsing failed")
    return text

# We use `flat_map` as `convert_to_text` has a 1->N relationship.
# It produces N strings for each PDF (one string per page).
# Use `map` for 1->1 relationship.
ds = ds.flat_map(convert_to_text)

from langchain.text_splitter import RecursiveCharacterTextSplitter

def split_text(page_text: str):
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000, chunk_overlap=100, length_function=len
    )
    split_text: List[str] = text_splitter.split_text(page_text)

    split_text = [text.replace("\n", " ") for text in split_text]
    return split_text

# We use `flat_map` as `split_text` has a 1->N relationship.
# It produces N output chunks for each input string.
# Use `map` for 1->1 relationship.
ds = ds.flat_map(split_text)

from sentence_transformers import SentenceTransformer

model_name = "sentence-transformers/all-mpnet-base-v2"

class Embed:
    def __init__(self):
        self.transformer = SentenceTransformer(model_name, device="cuda")

    def __call__(self, text_batch: List[str]):
        embeddings = self.transformer.encode(
            text_batch,
            batch_size=100,
            device="cuda",
        ).tolist()

        return list(zip(text_batch, embeddings))

# Use `map_batches` since we want to specify a batch size to maximize GPU utilization.
ds = ds.map_batches(
    Embed,
    batch_size=100,
    compute=ray.data.ActorPoolStrategy(min_size=20, max_size=20),
    num_gpus=1,
)

from langchain import FAISS
from langchain.embeddings import HuggingFaceEmbeddings

text_and_embeddings = []
for output in ds.iter_rows():
    text_and_embeddings.append(output)

print("Creating FAISS Vector Index.")
vector_store = FAISS.from_embeddings(
    text_and_embeddings,
    embedding=HuggingFaceEmbeddings(model_name=model_name),
)

print("Saving FAISS index locally.")
vector_store.save_local("faiss_index")
