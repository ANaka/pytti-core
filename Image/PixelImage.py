from pytti import *
from pytti.Image import DifferentiableImage
from pytti.LossAug import MSE_Loss
from pytti.ImageGuide import DirectImageGuide
import math
import numpy as np
import torch
from torch import nn, optim
from torch.nn import functional as F
from torchvision.transforms import functional as TF
from PIL import Image, ImageOps

def break_tensor(tensor):
  floors = tensor.floor().long()
  ceils  = tensor.ceil().long()
  rounds = tensor.round().long()
  fracs  = tensor - floors
  return floors, ceils, rounds, fracs

class PalletLoss(nn.Module):
  def __init__(self, pallet_size, n_pallets, gamma = 2.5, weight = 0.15, device=DEVICE):
    super().__init__()
    self.register_buffer('comp',torch.linspace(0,1,pallet_size).pow(gamma).view(pallet_size,1).repeat(1, n_pallets).to(device))
    self.register_buffer('weight',torch.as_tensor(weight).to(device))
  def forward(self, input):
    if isinstance(input, PixelImage):
      pallet = input.sort_pallet()
      magic_color = pallet.new_tensor([[[0.299,0.587,0.114]]])
      color_norms = torch.linalg.vector_norm(pallet * (magic_color.sqrt()), dim = -1)
      #print("color_norms",color_norms.shape)
      #print("self.comp",self.comp.shape)
      return F.mse_loss(color_norms, self.comp)*self.weight
    else:
      return 0
  
  def __str__(self):
    return "Pallet normalization"

class PixelImage(DifferentiableImage):
  """
  differentiable image format for pixel art images
  """
  def __init__(self, width, height, scale, pallet_size, n_pallets, gamma = 1, hdr_weight = 0.5, device=DEVICE):
    super().__init__(width*scale, height*scale)
    self.pallet_inertia = 2
    pallet = torch.linspace(0,self.pallet_inertia,pallet_size).pow(gamma).view(pallet_size,1,1).repeat(1,n_pallets,3)
    #pallet.set_(torch.rand_like(pallet)*self.pallet_inertia)
    self.pallet = nn.Parameter(pallet.to(device))

    self.pallet_size = pallet_size
    self.n_pallets = n_pallets
    self.value  = nn.Parameter(torch.zeros(height,width).to(device))
    self.tensor = nn.Parameter(torch.zeros(n_pallets, height, width).to(device))
    self.output_axes = ('n', 's', 'y', 'x')
    self.scale = scale
    self.pallet_loss = PalletLoss(pallet_size, n_pallets, gamma, hdr_weight) if hdr_weight != 0 else None

  
  def image_loss(self):
    return [self.pallet_loss] if self.pallet_loss is not None else []

  def sort_pallet(self):
    pallet = (self.pallet/self.pallet_inertia).clamp(0,1)
    #https://alienryderflex.com/hsp.html
    magic_color = pallet.new_tensor([[[0.299,0.587,0.114]]])
    color_norms = (pallet.square()*magic_color).sum(dim = -1)
    pallet_indices = color_norms.argsort(dim = 0).T
    pallet = torch.stack([pallet[i][:,j] for j,i in enumerate(pallet_indices)],dim=1)
    return pallet

  def get_image_tensor(self):
    return torch.cat([self.value.unsqueeze(0),self.tensor])

  @torch.no_grad()
  def set_image_tensor(self, tensor):
    self.value.set_(tensor[0])
    self.tensor.set_(tensor[1:])
  
  def decode_tensor(self):
    width, height = self.image_shape
    pallet = self.sort_pallet()

    #brightnes values of pixels
    values = self.value.clamp(0,1)*(self.pallet_size-1)
    value_floors, value_ceils, value_rounds, value_fracs = break_tensor(values)
    value_fracs = value_fracs.unsqueeze(-1).unsqueeze(-1)

    pallet_weights = self.tensor.movedim(0,2)
    pallets = F.one_hot(pallet_weights.argmax(dim = 2), num_classes=self.n_pallets)

    pallet_weights = pallet_weights.softmax(dim = 2).unsqueeze(-1)
    pallets = pallets.unsqueeze(-1)

    colors_disc = pallet[value_rounds]
    colors_disc = (colors_disc * pallets).sum(dim = 2)
    colors_disc = F.interpolate(colors_disc.movedim(2,0).unsqueeze(0), (height, width) , mode='nearest')

    colors_cont = pallet[value_floors]*(1-value_fracs) + pallet[value_ceils]*value_fracs
    colors_cont = (colors_cont * pallet_weights).sum(dim = 2)
    colors_cont = F.interpolate(colors_cont.movedim(2,0).unsqueeze(0), (height, width) , mode='nearest')
    return replace_grad(colors_disc, colors_cont*0.5+colors_disc*0.5)

  @torch.no_grad()
  def render_value_image(self):
    width, height = self.image_shape
    values = self.value.clamp(0,1).unsqueeze(-1).repeat(1,1,3)
    array = np.array(values.mul(255).clamp(0, 255).cpu().detach().numpy().astype(np.uint8))[:,:,:]
    return Image.fromarray(array).resize((width,height), Image.NEAREST)

  @torch.no_grad()
  def render_pallet(self):
    pallet = self.sort_pallet()
    width, height = self.n_pallets*16, self.pallet_size*32
    array = np.array(pallet.mul(255).clamp(0, 255).cpu().detach().numpy().astype(np.uint8))[:,:,:]
    return Image.fromarray(array).resize((width,height), Image.NEAREST)

  @torch.no_grad()
  def render_channel(self, pallet_i):
    width, height = self.image_shape
    pallet = self.sort_pallet()
    pallet[:,:pallet_i   ,:] = 0.5
    pallet[:, pallet_i+1:,:] = 0.5

    values = self.value.clamp(0,1)*(self.pallet_size-1)
    value_floors, value_ceils, value_rounds, value_fracs = break_tensor(values)
    value_fracs = value_fracs.unsqueeze(-1).unsqueeze(-1)

    pallet_weights = self.tensor.movedim(0,2)
    pallets = F.one_hot(pallet_weights.argmax(dim = 2), num_classes=self.n_pallets)
    pallet_weights = pallet_weights.softmax(dim = 2).unsqueeze(-1)

    colors_cont = pallet[value_floors]*(1-value_fracs) + pallet[value_ceils]*value_fracs
    colors_cont = (colors_cont * pallet_weights).sum(dim = 2)
    colors_cont = F.interpolate(colors_cont.movedim(2,0).unsqueeze(0), (height, width) , mode='nearest')

    tensor = named_rearrange(colors_cont, self.output_axes, ('y', 'x', 's'))
    array = np.array(tensor.mul(255).clamp(0, 255).cpu().detach().numpy().astype(np.uint8))[:,:,:]
    return Image.fromarray(array)

  @torch.no_grad()
  def update(self):
    self.pallet.clamp_(0,self.pallet_inertia)
    self.value.clamp_(0,1)
    self.tensor.clamp_(0,float('inf'))
    #self.tensor.set_(self.tensor.softmax(dim = 0))

  def encode_image(self, pil_image, device=DEVICE):
    width, height = self.image_shape

    scale = self.scale
    color_ref = pil_image.resize((width//scale, height//scale), Image.LANCZOS)
    #value_ref = ImageOps.grayscale(color_ref)
    with torch.no_grad():
        #https://alienryderflex.com/hsp.html
        magic_color = self.pallet.new_tensor([[[0.299]],[[0.587]],[[0.114]]])
        value_ref = torch.linalg.vector_norm(TF.to_tensor(color_ref).to(device)*(magic_color.sqrt()), dim=0)
        self.value.set_(value_ref)
        

    #no embedder needed without any prompts
    pil_image = pil_image.resize((width,height), Image.LANCZOS)
    target = TF.to_tensor(pil_image).to(device)
    mse = MSE_Loss(target)
    guide = DirectImageGuide(self, None, optimizer = optim.Adam([self.pallet, self.tensor], lr = .1))
    guide.run_steps(201,[],[],[],[mse])



  @torch.no_grad()
  def encode_random(self, random_pallet = False):
    self.value.uniform_()
    self.tensor.uniform_()
    if random_pallet:
      self.pallet.uniform_(to=self.pallet_inertia)

