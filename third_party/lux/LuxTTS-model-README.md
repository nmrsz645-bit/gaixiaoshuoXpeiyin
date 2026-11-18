---
license: apache-2.0
language:
- en
pipeline_tag: text-to-speech
---

## LuxTTS
<p align="center">
  <a href="https://huggingface.co/YatharthS/LuxTTS">
    <img src="https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Model-FFD21E" alt="Hugging Face Model">
  </a>
  &nbsp;
  <a href="https://colab.research.google.com/drive/1cDaxtbSDLRmu6tRV_781Of_GSjHSo1Cu?usp=sharing">
    <img src="https://img.shields.io/badge/Colab-Notebook-F9AB00?logo=googlecolab&logoColor=white" alt="Colab Notebook">
  </a>
</p>

This is the model for LuxTTS, a lightweight zipvoice based text-to-speech model designed for high quality voice cloning and realistic generation at speeds exceeding 150x realtime.

### Main features
- Voice cloning: SOTA voice cloning on par with models 10x larger.
- Clarity: Clear 48khz speech generation unlike most TTS models which are limited to 24khz.
- Speed: Reaches speeds of 150x realtime on a single GPU and faster then realtime on CPU's as well.
- Efficiency: Fits within 1gb vram meaning it can fit in any local gpu.


### Details
- Based on ZipVoice, distilled to 4steps.
- Uses 48khz vocoder instead of 24khz vocoder.
- Implemented higher quality sampling technique then standard euler.


### Usage

Please check out the repo for usage: https://github.com/ysharma3501/LuxTTS.git

### License
Model and code is released under Apache-2.0 license. 

If you find the model/code helpful, stars or likes would be appreciated. Thank you.