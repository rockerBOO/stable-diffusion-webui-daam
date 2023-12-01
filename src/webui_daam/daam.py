from __future__ import annotations

import re
from itertools import chain
from pathlib import Path
from pprint import pprint

import gradio as gr
import matplotlib
import modules.scripts as scripts
import modules.shared as shared
import open_clip.tokenizer
import torch
from daam import trace
from ldm.modules.encoders.modules import FrozenOpenCLIPEmbedder
from modules import (
    script_callbacks,
    sd_hijack_clip,
    sd_hijack_open_clip,
)
from modules.images import image_grid, resize_image, save_image
from modules.processing import (
    StableDiffusionProcessing,
    fix_seed,
)
from modules.sd_hijack_clip import (
    FrozenCLIPEmbedderWithCustomWordsBase,
)
from modules.sd_hijack_open_clip import FrozenOpenCLIPEmbedderWithCustomWords
from modules.shared import opts
from transformers.image_transforms import to_pil_image

from webui_daam.log import debug, info, warning, error, log
from webui_daam.image import (
    create_heatmap_image_overlay,
)

matplotlib.use("Agg")

global before_image_saved_handler
before_image_saved_handler = None

addnet_paste_params = {"txt2img": [], "img2img": []}


class Script(scripts.Script):
    GRID_LAYOUT_AUTO = "Auto"
    GRID_LAYOUT_PREVENT_EMPTY = "Prevent Empty Spot"
    GRID_LAYOUT_BATCH_LENGTH_AS_ROW = "Batch Length As Row"

    def title(self):
        return "DAAM script"

    def describe(self):
        return """
        Description of the DAAM script
        """

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def run(
        self,
        p: StableDiffusionProcessing,
        attention_texts: str,
        enabled: bool,
        show_images: bool,
        save_images: bool,
        show_caption: bool,
        use_grid: bool,
        grid_layout: str,
        alpha: float,
        heatmap_image_scale: float,
        trace_each_layers: bool,
        layers_as_row: bool,
    ):
        print("RUN!")

    def ui(self, is_img2img):
        with gr.Group():
            with gr.Accordion("Attention Heatmap", open=False):
                with gr.Row():
                    attention_texts = gr.Textbox(
                        placeholder="Attention texts (required)",
                        value="",
                        info="Comma separated. Must be in prompt.",
                        show_label=False,
                        scale=4,
                    )
                    enabled = gr.Checkbox(
                        label="Enabled",
                        value=True,
                        info="Enable tracing the images",
                    )

                with gr.Row():
                    show_images = gr.Checkbox(
                        label="Show heatmap images",
                        value=True,
                        info="Show images in the output area",
                        show_label=False,
                    )

                    save_images = gr.Checkbox(
                        label="Save heatmap images",
                        value=True,
                        info="Save images to the output directory",
                        show_label=False,
                    )

                    show_caption = gr.Checkbox(
                        label="Show caption",
                        value=True,
                        info="Show captions on top of the images",
                        show_label=False,
                    )

                with gr.Row(elem_classes="row-spacer"):
                    use_grid = gr.Checkbox(
                        label="Use grid",
                        value=False,
                        info="Output to grid dir",
                    )

                    grid_per_image = gr.Checkbox(
                        label="Grid per image",
                        value=True,
                        info="Attention heatmap grid per image",
                    )

                    grid_layout = gr.Dropdown(
                        [
                            Script.GRID_LAYOUT_AUTO,
                            Script.GRID_LAYOUT_PREVENT_EMPTY,
                            Script.GRID_LAYOUT_BATCH_LENGTH_AS_ROW,
                        ],
                        label="Grid layout",
                        value=Script.GRID_LAYOUT_AUTO,
                    )

                with gr.Row(elem_classes="row-spacer"):
                    alpha = gr.Slider(
                        label="Heatmap blend alpha",
                        value=0.5,
                        minimum=0,
                        maximum=1,
                        step=0.01,
                    )

                    heatmap_image_scale = gr.Slider(
                        label="Heatmap image scale",
                        value=1.0,
                        minimum=0.1,
                        maximum=1,
                        step=0.025,
                    )

                with gr.Row(visible=False):
                    trace_each_layers = gr.Checkbox(
                        label="Trace IN MID OUT blocks",
                        value=False,
                    )

                    layers_as_row = gr.Checkbox(
                        label="Use layers as row instead of Batch Length",
                        value=False,
                    )

        return [
            attention_texts,
            enabled,
            show_images,
            save_images,
            show_caption,
            use_grid,
            grid_per_image,
            grid_layout,
            alpha,
            heatmap_image_scale,
            trace_each_layers,
            layers_as_row,
            # False,  # disabling trace for now
            # False,
        ]

    @torch.no_grad()
    def process(
        self,
        p: StableDiffusionProcessing,
        attention_texts: str,
        enabled: bool,
        show_images: bool,
        save_images: bool,
        show_caption: bool,
        use_grid: bool,
        grid_per_image: bool,
        grid_layout: str,
        alpha: float,
        heatmap_image_scale: float,
        trace_each_layers: bool,
        layers_as_row: bool,
    ):
        self.enabled = False  # in case the assert fails

        def handle_before_image_saved(params):
            self.trace_each_layers = trace_each_layers
            self.before_image_saved(params)

        global before_image_saved_handler
        before_image_saved_handler = handle_before_image_saved

        self.debug("DAAM Process...")

        self.debug(f"attention text {attention_texts}")
        assert opts.samples_save, (
            "Cannot run Daam script. Enable "
            + "Always save all generated images' setting."
        )

        self.images = []
        self.show_images = show_images
        self.save_images = save_images
        self.show_caption = show_caption
        self.alpha = alpha
        self.use_grid = use_grid
        self.grid_layout = grid_layout
        self.heatmap_image_scale = heatmap_image_scale
        self.heatmap_images = dict()
        self.global_heatmaps = []

        self.attentions = [
            s.strip()
            for s in attention_texts.split(",")
            if s.strip() and len(s.strip()) > 0
        ]
        self.enabled = len(self.attentions) > 0 and enabled
        self.trace = None

        fix_seed(p)

    def get_tokenizer(self, p):
        if isinstance(
            p.sd_model.cond_stage_model.wrapped, FrozenOpenCLIPEmbedder
        ):
            return Tokenizer(open_clip.tokenizer._tokenizer.encode)

        return Tokenizer(
            p.sd_model.cond_stage_model.wrapped.tokenizer.tokenize
        )

    def tokenize(self, p, prompt):
        tokenizer = self.get_tokenizer(p)

        return tokenizer.tokenize(prompt)

    def get_context_size(self, p: StableDiffusionProcessing, prompt: str):
        embedder = None
        if isinstance(
            p.sd_model.cond_stage_model,
            sd_hijack_clip.FrozenCLIPEmbedderWithCustomWords,
        ) or isinstance(
            p.sd_model.cond_stage_model,
            sd_hijack_open_clip.FrozenOpenCLIPEmbedderWithCustomWords,
        ):
            embedder = p.sd_model.cond_stage_model
        else:
            assert False, (
                f"Embedder '{type(p.sd_model.cond_stage_model)}' "
                + "is not supported."
            )

        tokens = self.tokenize(p, escape_prompt(prompt))
        self.debug(f"DAAM tokens: {tokens}")
        context_size = calc_context_size(len(tokens))

        prompt_analyzer = PromptAnalyzer(embedder, prompt)
        self.prompt_analyzer = prompt_analyzer
        context_size = prompt_analyzer.context_size

        self.debug(
            f"daam run with context_size={prompt_analyzer.context_size}, "
            + f"token_count={prompt_analyzer.token_count}"
        )
        self.debug(
            f"remade_tokens={prompt_analyzer.tokens}, "
            + f"multipliers={prompt_analyzer.multipliers}"
        )
        self.debug(
            f"hijack_comments={prompt_analyzer.hijack_comments}, "
            + f"used_custom_terms={prompt_analyzer.used_custom_terms}"
        )
        self.debug(f"fixes={prompt_analyzer.fixes}")

        return context_size

    @torch.no_grad()
    def process_batch(
        self,
        p: StableDiffusionProcessing,
        attention_texts: str,
        enabled: bool,
        show_images: bool,
        save_images: bool,
        show_caption: bool,
        use_grid: bool,
        grid_per_image: bool,
        grid_layout: str,
        alpha: float,
        heatmap_image_scale: float,
        trace_each_layers: bool,
        layers_as_row: bool,
        prompts,
        **kwargs,
    ):
        self.debug("Process batch")
        if not self.is_enabled(attention_texts, enabled):
            self.debug("not enabled")
            return

        self.debug("Processing batch...")

        styled_prompt = prompts[0]

        context_size = self.get_context_size(p, styled_prompt)

        if any(
            item[0] in self.attentions
            for item in self.prompt_analyzer.used_custom_terms
        ):
            info("Embedding heatmap cannot be shown.")

        tokenizer = self.get_tokenizer(p)

        self.trace = trace(
            unet=p.sd_model.model.diffusion_model,
            vae=p.sd_model.first_stage_model,
            vae_scale_factor=8,
            tokenizer=tokenizer,
            width=p.width,
            height=p.height,
            context_size=context_size,
            sample_size=64,  # TODO: Update to proper sample size
            image_processor=to_pil_image,
            batch_size=p.batch_size,
        )

        info(
            f"Trace attention heatmaps {','.join(self.attentions)} "
            + f"for prompt {styled_prompt}"
        )

        self.heatmap_blend_alpha = alpha

        self.trace.hook()

        # self.set_infotext_fields(p, self.latest_params)

    @torch.no_grad()
    def postprocess(
        self,
        p: StableDiffusionProcessing,
        processed,
        attention_texts: str,
        enabled: bool,
        show_images: bool,
        save_images: bool,
        show_caption: bool,
        use_grid: bool,
        grid_per_image: bool,
        grid_layout: str,
        alpha: float,
        heatmap_image_scale: float,
        trace_each_layers: bool,
        layers_as_row: bool,
        **kwargs,
    ):
        debug("Postprocess kwargs", kwargs)
        debug("postprocess...")
        if self.is_enabled(attention_texts, enabled) is False:
            debug("disabled...")
            return

        self.try_unhook()

        initial_info = None

        if initial_info is None:
            initial_info = processed.info

        images = processed.images

        # print("PROCESSED ----")
        # pprint(vars(processed))

        # Disable the handler from handling the hooking into the next images
        global before_image_saved_handler
        before_image_saved_handler = None

        # if layers_as_row:
        #     heatmap_images = []
        #     for k in sorted(self.heatmap_images.keys()):
        #         imgs += [
        #             self.heatmap_images[k][len(self.attentions) * i + j]
        #             for j in range(len(self.attentions))
        #         ]
        #     heatmap_images.extend(imgs)
        # else:

        # heatmap_images = self.heatmap_images.keys()

        if len(self.heatmap_images.keys()) != len(images):
            # print(
            #     "heatmap images",
            #     len(self.heatmap_images.keys()),
            #     self.heatmap_images.keys(),
            # )
            # print(len(heatmap_images), len(images), heatmap_images, images)
            self.debug(
                "Invalid result of images... images_list: "
                + f"{len(self.heatmap_images.keys())} images: {len(images)}"
            )

        self.debug(f"Heatmap images: {len(self.heatmap_images.keys())}")
        self.debug(f"Images: {len(images)}")

        debug(f"processed images: {processed.images}")

        all_images = []

        for (seed, heatmap_images), img in zip(
            self.heatmap_images.items(), images
        ):
            self.debug(
                f"Processing seed {seed} heatmap_images {heatmap_images} img {img}"
            )

            # Add grid image
            if heatmap_images and self.use_grid:
                grid_img = self.make_grid(p, heatmap_images, layers_as_row)

                if show_images:
                    processed.images.insert(0, grid_img)
                    processed.index_of_first_image += 1
                    processed.infotexts.insert(0, processed.infotexts[0])

            if show_images:
                processed.images[:0] = heatmap_images
                processed.index_of_first_image += len(heatmap_images)
                processed.infotexts[:0] = [processed.infotexts[0]] * len(
                    heatmap_images
                )

            # Resize image...
            if trace_each_layers:
                save_image_resized = resize_image(
                    resize_mode=0,
                    im=img,
                    width=heatmap_images[0].size[0],
                    height=heatmap_images[0].size[1],
                )

                img_heatmap_grid_img = self.make_grid(
                    p,
                    heatmap_images + [save_image_resized],
                )
            else:
                save_image_resized = resize_image(
                    resize_mode=0,
                    im=img,
                    width=heatmap_images[0].size[0],
                    height=heatmap_images[0].size[1],
                )

                img_heatmap_grid_img = self.make_grid(
                    p,
                    heatmap_images + [save_image_resized],
                )

            if show_images and grid_per_image:
                # Insert grid images into processed list
                processed.images.insert(0, img_heatmap_grid_img)
                processed.index_of_first_image += 1
                processed.infotexts.insert(0, processed.infotexts[0])

        return processed

    def is_enabled(self, attention_texts, enabled):
        if self.enabled is False:
            return False

        if enabled is False:
            return False

        if attention_texts == "":
            return False

        return True

    def make_grid(self, p, img_list, layers_as_row=False):
        grid_layout = self.grid_layout
        if grid_layout == Script.GRID_LAYOUT_AUTO:
            if p.batch_size * p.n_iter == 1:
                grid_layout = Script.GRID_LAYOUT_PREVENT_EMPTY
            else:
                grid_layout = Script.GRID_LAYOUT_BATCH_LENGTH_AS_ROW

        if grid_layout == Script.GRID_LAYOUT_PREVENT_EMPTY:
            grid_img = image_grid(img_list)
        elif grid_layout == Script.GRID_LAYOUT_BATCH_LENGTH_AS_ROW:
            if layers_as_row:
                batch_size = len(self.attentions)
                rows = len(self.heatmap_images.keys())
            else:
                batch_size = p.batch_size
                rows = p.batch_size * p.n_iter
            grid_img = image_grid(img_list, batch_size=batch_size, rows=rows)
        else:
            raise RuntimeError(f"Invalid grid layout: {grid_layout}")

        if self.save_images:
            save_image(grid_img, p.outpath_grids, "grid_daam", grid=True, p=p)

        return grid_img

    def set_infotext_fields(self, p, params):
        pass
        # p.extra_generation_params.update(
        #     {
        #         f"AddNet Weight B {i+1}": weight_tenc,
        #     }
        # )

    @torch.no_grad()
    def before_image_saved(self, params: script_callbacks.ImageSaveParams):
        debug("Before image saved...")

        # pprint(vars(params))

        if (
            "txt2img-grid" in params.filename
            or "img2img-grid" in params.filename
        ):
            debug("Skipping because it's a grid. Maybe not ideal?")
            return

        if self.trace is None:
            debug("No trace")

        if len(self.attentions) == 0:
            debug("No attentions to heatmap")

        if self.trace is None or len(self.attentions) == 0:
            return

        prompt = shared.prompt_styles.apply_styles_to_prompt(
            params.p.prompt, params.p.styles
        )

        debug(
            f"batch_index: {params.p.batch_index} "
            + f"batch_size: {params.p.batch_size}"
        )
        # print(
        #     f"{params.p}, {self.trace}, {prompt}, "
        #     + f"{self.trace_each_layers}"
        # )
        if params.p.batch_index == 0:
            self.global_heat_maps = calc_global_heatmap(
                params.p, self.trace, prompt, self.trace_each_layers
            )

        seed = params.p.seeds[params.p.batch_index]

        for global_heat_map in self.global_heat_maps:
            debug(
                f"Global heatmap ({len(global_heat_map.heat_maps)}) "
                + f"for {global_heat_map.prompt} "
            )
            heatmap_images = []

            for attention in self.attentions:
                debug(
                    f"Batch id: {params.p.batch_index} "
                    + f"attention: {attention}"
                )

                img = create_heatmap_image_overlay(
                    global_heat_map,
                    attention,
                    image=params.image,
                    show_word=self.show_caption,
                    alpha=self.heatmap_blend_alpha,
                    batch_idx=params.p.batch_index,
                    opts=opts,
                )

                heatmap_images.append(img)

                if self.save_images:
                    filename = Path(params.filename)
                    attention_caption_filename = filename.with_name(
                        f"{filename.stem}_{attention}{filename.suffix}"
                    )

                    img.save(attention_caption_filename)

            self.heatmap_images[seed] = heatmap_images

        if len(self.heatmap_images[seed]) == 0:
            info("DAAM: Did not create any heatmap images.")

        # self.heatmap_images = {
        #     j: self.heatmap_images[j]
        #     for j in self.heatmap_images.keys()
        #     if self.heatmap_images[j]
        # }

        # if it is last batch pos, clear heatmaps
        # if batch_pos == params.p.batch_size - 1:
        #     for tracer in self.traces:
        #         tracer.reset()

        self.try_unhook()
        return params

    def try_unhook(self):
        if self.trace is not None:
            try:
                self.trace.unhook()
            # Possibly not hooked and we are only attempting to unhook
            except RuntimeError:
                pass

    def debug(self, message):
        debug(message)

    def log(self, message):
        log(message)

    def error(self, err, message):
        error(err, message)

    def warning(self, err, message):
        warning(err, message)

    def __getattr__(self, attr):
        warning("unknown call", attr)
        # import traceback
        # traceback.print_stack()


def calc_global_heatmap(
    p: StableDiffusionProcessing, trace, prompt, trace_each_layers=False
):
    try:
        num_input = len(p.sd_model.model.diffusion_model.input_blocks)
        num_output = len(p.sd_model.model.diffusion_model.output_blocks)
        if trace_each_layers:
            global_heatmaps = [
                trace.compute_global_heat_map(prompt, layer_i_mapdx=layer_idx)
                for layer_idx in range(num_input + 1 + num_output)
            ]
        else:
            global_heatmaps = [trace.compute_global_heat_map(prompt)]
    except RuntimeError as err:
        warning(
            err,
            "DAAM: Failed to get computed global heatmap for " + f" {prompt}",
        )
        return []

    return global_heatmaps


@torch.no_grad()
def on_before_image_saved(params):
    global before_image_saved_handler
    if before_image_saved_handler is not None:
        return before_image_saved_handler(params)

    return


def on_script_unloaded():
    if shared.sd_model:
        for s in scripts.scripts_txt2img.alwayson_scripts:
            if isinstance(s, Script):
                s.try_unhook()
                break


def on_infotext_pasted(infotext, params):
    pass
    # if "AddNet Enabled" not in params:
    #     params["AddNet Enabled"] = "False"
    #
    # # TODO changing "AddNet Separate Weights" does not seem to work
    # if "AddNet Separate Weights" not in params:
    #     params["AddNet Separate Weights"] = "False"
    #
    # for i in range(MAX_MODEL_COUNT):
    #     # Convert combined weight into new format
    #     if f"AddNet Weight {i+1}" in params:
    #         params[f"AddNet Weight A {i+1}"] = params[f"AddNet Weight {i+1}"]
    #         params[f"AddNet Weight B {i+1}"] = params[f"AddNet Weight {i+1}"]
    #
    #     if f"AddNet Module {i+1}" not in params:
    #         params[f"AddNet Module {i+1}"] = "LoRA"
    #     if f"AddNet Model {i+1}" not in params:
    #         params[f"AddNet Model {i+1}"] = "None"
    #     if f"AddNet Weight A {i+1}" not in params:
    #         params[f"AddNet Weight A {i+1}"] = "0"
    #     if f"AddNet Weight B {i+1}" not in params:
    #         params[f"AddNet Weight B {i+1}"] = "0"
    #
    #     params[f"AddNet Weight {i+1}"] = params[f"AddNet Weight A {i+1}"]
    #
    #     if (
    #         params[f"AddNet Weight A {i+1}"]
    #         != params[f"AddNet Weight B {i+1}"]
    #     ):
    #         params["AddNet Separate Weights"] = "True"
    #
    #     # Convert potential legacy name/hash to new format
    #     params[f"AddNet Model {i+1}"] = str(
    #         model_util.find_closest_lora_model_name(
    #             params[f"AddNet Model {i+1}"]
    #         )
    #     )
    #
    #     addnet_xyz_grid_support.update_axis_params(
    #         i, params[f"AddNet Module {i+1}"], params[f"AddNet Model {i+1}"]
    #     )


script_callbacks.on_before_image_saved(on_before_image_saved)
script_callbacks.on_infotext_pasted(on_infotext_pasted)


# Emulating hugging face tokenizer
class Tokenizer:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def tokenize(self, prompt):
        return self.tokenizer(prompt)


def calc_context_size(token_length: int):
    len_check = 0 if (token_length - 1) < 0 else token_length - 1
    return ((int)(len_check // 75) + 1) * 77


def escape_prompt(prompt):
    if isinstance(prompt, str):
        prompt = prompt.lower()
        prompt = re.sub(r"[\(\)\[\]]", "", prompt)
        prompt = re.sub(r":\d+\.*\d*", "", prompt)
        return prompt
    elif isinstance(prompt, list):
        prompt_new = []
        for i in range(len(prompt)):
            prompt_new.append(escape_prompt(prompt[i]))
        return prompt_new


class PromptAnalyzer:
    def __init__(self, clip: FrozenCLIPEmbedderWithCustomWordsBase, text: str):
        use_old = opts.use_old_emphasis_implementation
        assert not use_old, "use_old_emphasis_implementation is not supported"

        self.clip = clip
        self.id_start = clip.id_start
        self.id_end = clip.id_end
        self.is_open_clip = (
            True
            if isinstance(clip, FrozenOpenCLIPEmbedderWithCustomWords)
            else False
        )
        self.used_custom_terms = []
        self.hijack_comments = []

        chunks, token_count = self.tokenize_line(text)

        self.token_count = token_count
        self.fixes = list(chain.from_iterable(chunk.fixes for chunk in chunks))
        self.context_size = calc_context_size(token_count)

        tokens = list(chain.from_iterable(chunk.tokens for chunk in chunks))
        multipliers = list(
            chain.from_iterable(chunk.multipliers for chunk in chunks)
        )

        self.tokens = []
        self.multipliers = []
        for i in range(self.context_size // 77):
            self.tokens.extend(
                [self.id_start] + tokens[i * 75 : i * 75 + 75] + [self.id_end]
            )
            self.multipliers.extend(
                [1.0] + multipliers[i * 75 : i * 75 + 75] + [1.0]
            )

    def create(self, text: str):
        return PromptAnalyzer(self.clip, text)

    def tokenize_line(self, line):
        chunks, token_count = self.clip.tokenize_line(line)
        return chunks, token_count

    def process_text(self, texts):
        (
            batch_multipliers,
            remade_batch_tokens,
            used_custom_terms,
            hijack_comments,
            hijack_fixes,
            token_count,
        ) = self.clip.process_text(texts)
        return (
            batch_multipliers,
            remade_batch_tokens,
            used_custom_terms,
            hijack_comments,
            hijack_fixes,
            token_count,
        )

    def encode(self, text: str):
        return self.clip.tokenize([text])[0]

    def calc_word_indecies(self, word: str, limit: int = -1, start_pos=0):
        word = word.lower()
        merge_idxs = []

        tokens = self.tokens
        needles = self.encode(word)

        limit_count = 0
        current_pos = 0
        for i, token in enumerate(tokens):
            current_pos = i
            if i < start_pos:
                continue

            if needles[0] == token and len(needles) > 1:
                next = i + 1
                success = True
                for needle in needles[1:]:
                    if next >= len(tokens) or needle != tokens[next]:
                        success = False
                        break
                    next += 1

                # append consecutive indexes if all pass
                if success:
                    merge_idxs.extend(list(range(i, next)))
                    if limit > 0:
                        limit_count += 1
                        if limit_count >= limit:
                            break

            elif needles[0] == token:
                merge_idxs.append(i)
                if limit > 0:
                    limit_count += 1
                    if limit_count >= limit:
                        break

        return merge_idxs, current_pos
